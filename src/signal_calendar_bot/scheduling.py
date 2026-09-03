"""Interval math and slot generation. Pure functions, no I/O, no model.

The LLM hands over a window and a duration. Everything from there — merging busy
blocks, applying buffers, honoring day bounds and protected recurring blocks,
picking candidates — happens here, deterministically, and is unit-testable
without Ollama or Google running.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from .config import SchedulingConfig
from .models import TimeSlot

Interval = tuple[datetime, datetime]


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """Sort and coalesce overlapping/adjacent intervals."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: iv[0])
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            if end > last_end:
                merged[-1] = (last_start, end)
        else:
            merged.append((start, end))
    return merged


def pad_intervals(intervals: list[Interval], buffer_minutes: int) -> list[Interval]:
    """Grow each busy block by the buffer on both sides, then re-merge."""
    if buffer_minutes <= 0:
        return merge_intervals(intervals)
    delta = timedelta(minutes=buffer_minutes)
    return merge_intervals([(s - delta, e + delta) for s, e in intervals])


def subtract_intervals(window: Interval, busy: list[Interval]) -> list[Interval]:
    """Return the parts of `window` not covered by any busy interval."""
    window_start, window_end = window
    free: list[Interval] = []
    cursor = window_start
    for busy_start, busy_end in merge_intervals(busy):
        if busy_end <= cursor:
            continue
        if busy_start >= window_end:
            break
        if busy_start > cursor:
            free.append((cursor, min(busy_start, window_end)))
        cursor = max(cursor, busy_end)
        if cursor >= window_end:
            break
    if cursor < window_end:
        free.append((cursor, window_end))
    return [(s, e) for s, e in free if e > s]


def intersect_intervals(a: list[Interval], b: list[Interval]) -> list[Interval]:
    """Intersection of two interval lists."""
    out: list[Interval] = []
    for a_start, a_end in merge_intervals(a):
        for b_start, b_end in merge_intervals(b):
            start, end = max(a_start, b_start), min(a_end, b_end)
            if start < end:
                out.append((start, end))
    return merge_intervals(out)


def _local_dates(window: Interval, tz: ZoneInfo) -> list[date]:
    """Every local calendar date the (UTC) window touches."""
    start_local = window[0].astimezone(tz)
    end_local = window[1].astimezone(tz)
    days: list[date] = []
    cursor = start_local.date()
    while cursor <= end_local.date():
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _local_span(day: date, start: dt_time, end: dt_time, tz: ZoneInfo) -> Interval:
    """A local-time span on a given day, returned in UTC.

    Handles DST by letting zoneinfo resolve the local wall time; a span that
    crosses midnight (end <= start) rolls into the following day.
    """
    begin = datetime.combine(day, start, tzinfo=tz)
    finish_day = day if end > start else day + timedelta(days=1)
    finish = datetime.combine(finish_day, end, tzinfo=tz)
    return (begin.astimezone(UTC), finish.astimezone(UTC))


def working_hours(window: Interval, cfg: SchedulingConfig, tz: ZoneInfo) -> list[Interval]:
    """The bookable part of each day inside the window, in UTC."""
    spans = [
        _local_span(day, cfg.day_start_time, cfg.day_end_time, tz)
        for day in _local_dates(window, tz)
    ]
    return intersect_intervals(spans, [window])


def protected_intervals(window: Interval, cfg: SchedulingConfig, tz: ZoneInfo) -> list[Interval]:
    """Recurring blocks (jiu-jitsu, classes) expanded across the window, in UTC."""
    out: list[Interval] = []
    for block in cfg.protected_blocks:
        weekdays = block.weekdays
        for day in _local_dates(window, tz):
            if day.weekday() in weekdays:
                out.append(_local_span(day, block.start_time, block.end_time, tz))
    return merge_intervals(out)


def _ceil_to_granularity(moment: datetime, minutes: int, tz: ZoneInfo) -> datetime:
    """Round a UTC instant up to the next local :00/:15/:30/:45 boundary."""
    if minutes <= 0:
        return moment
    local = moment.astimezone(tz)
    on_boundary = local.second == 0 and local.microsecond == 0
    remainder = (local.minute % minutes) or (0 if on_boundary else minutes)
    if remainder:
        local += timedelta(minutes=minutes - remainder)
    local = local.replace(second=0, microsecond=0)
    return local.astimezone(UTC)


def free_intervals(
    window: Interval,
    busy: list[Interval],
    cfg: SchedulingConfig,
    tz: ZoneInfo,
    *,
    apply_rules: bool = True,
) -> list[Interval]:
    """Everything bookable in the window after busy blocks and rules are removed."""
    blocked = pad_intervals(list(busy), cfg.buffer_minutes if apply_rules else 0)
    if apply_rules:
        # Protected blocks get the buffer too — nothing brushes up against them.
        blocked = merge_intervals(
            blocked + pad_intervals(protected_intervals(window, cfg, tz), cfg.buffer_minutes)
        )
        bookable = working_hours(window, cfg, tz)
    else:
        bookable = [window]

    free: list[Interval] = []
    for span in bookable:
        free.extend(subtract_intervals(span, blocked))
    return merge_intervals(free)


def find_slots(
    window: Interval,
    busy: list[Interval],
    duration_minutes: int,
    cfg: SchedulingConfig,
    tz: ZoneInfo,
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[TimeSlot]:
    """Candidate start times of `duration_minutes` inside the window.

    At most one candidate per local day so three options are three different
    days rather than 09:00/09:15/09:30 on the same morning.
    """
    now = now or datetime.now(UTC)
    limit = limit if limit is not None else cfg.max_candidates
    duration = timedelta(minutes=duration_minutes)

    # Never propose a slot in the past.
    window = (max(window[0], now), window[1])
    if window[1] <= window[0]:
        return []

    candidates: list[TimeSlot] = []
    seen_days: set[date] = set()
    for free_start, free_end in free_intervals(window, busy, cfg, tz):
        cursor = _ceil_to_granularity(free_start, cfg.slot_granularity_minutes, tz)
        while cursor + duration <= free_end:
            day = cursor.astimezone(tz).date()
            if day not in seen_days:
                candidates.append(TimeSlot(start=cursor, end=cursor + duration))
                seen_days.add(day)
                break
            cursor += timedelta(minutes=cfg.slot_granularity_minutes)
        if len(candidates) >= limit:
            break
    return candidates[:limit]


def conflicts_for(slot: Interval, busy: list[Interval]) -> list[Interval]:
    """Busy blocks that genuinely overlap the slot (no buffer applied)."""
    start, end = slot
    return [(b_start, b_end) for b_start, b_end in busy if b_start < end and b_end > start]


def violates_rules(slot: Interval, cfg: SchedulingConfig, tz: ZoneInfo) -> list[str]:
    """Human-readable reasons an exact requested time breaks the scheduling rules.

    Used for `exact_time` requests: the user named a time, so code does not move
    it, but the preview says what it is stepping on.
    """
    reasons: list[str] = []

    within_hours = intersect_intervals([slot], working_hours(slot, cfg, tz))
    covered = sum((e - s for s, e in within_hours), timedelta())
    if covered < (slot[1] - slot[0]):
        reasons.append(f"outside {cfg.day_start}-{cfg.day_end}")

    for block in cfg.protected_blocks:
        weekdays = block.weekdays
        for day in _local_dates(slot, tz):
            if day.weekday() not in weekdays:
                continue
            b_start, b_end = _local_span(day, block.start_time, block.end_time, tz)
            if b_start < slot[1] and b_end > slot[0]:
                reasons.append(f"overlaps protected block {block.name!r}")
                break

    return reasons
