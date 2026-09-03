"""Slot math. No model, no network — this is the part that must be right."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from signal_calendar_bot.scheduling import (
    find_slots,
    free_intervals,
    merge_intervals,
    pad_intervals,
    subtract_intervals,
    violates_rules,
)

TZ = ZoneInfo("America/New_York")


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def local(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=TZ).astimezone(UTC)


def test_merge_coalesces_overlaps():
    merged = merge_intervals(
        [
            (utc(2026, 9, 10, 9), utc(2026, 9, 10, 10)),
            (utc(2026, 9, 10, 9, 30), utc(2026, 9, 10, 11)),
        ]
    )
    assert merged == [(utc(2026, 9, 10, 9), utc(2026, 9, 10, 11))]


def test_subtract_carves_out_the_middle():
    free = subtract_intervals(
        (utc(2026, 9, 10, 9), utc(2026, 9, 10, 17)),
        [(utc(2026, 9, 10, 12), utc(2026, 9, 10, 13))],
    )
    assert free == [
        (utc(2026, 9, 10, 9), utc(2026, 9, 10, 12)),
        (utc(2026, 9, 10, 13), utc(2026, 9, 10, 17)),
    ]


def test_buffer_grows_busy_blocks_both_sides():
    padded = pad_intervals([(utc(2026, 9, 10, 12), utc(2026, 9, 10, 13))], 15)
    assert padded == [(utc(2026, 9, 10, 11, 45), utc(2026, 9, 10, 13, 15))]


def test_free_intervals_respect_day_bounds(scheduling_cfg):
    window = (local(2026, 9, 10, 0), local(2026, 9, 11, 0))  # Thursday
    free = free_intervals(window, [], scheduling_cfg, TZ)
    assert free == [(local(2026, 9, 10, 9), local(2026, 9, 10, 21))]


def test_protected_block_is_never_offered(scheduling_cfg):
    """Friday has jiu-jitsu 18:30-20:00; with buffers, 18:15-20:15 is gone."""
    window = (local(2026, 9, 11, 0), local(2026, 9, 12, 0))  # Friday
    free = free_intervals(window, [], scheduling_cfg, TZ)
    assert free == [
        (local(2026, 9, 11, 9), local(2026, 9, 11, 18, 15)),
        (local(2026, 9, 11, 20, 15), local(2026, 9, 11, 21)),
    ]


def test_busy_block_pushes_the_slot_past_the_buffer(scheduling_cfg):
    window = (local(2026, 9, 10, 0), local(2026, 9, 11, 0))
    busy = [(local(2026, 9, 10, 9), local(2026, 9, 10, 12))]
    slots = find_slots(window, busy, 60, scheduling_cfg, TZ, now=utc(2026, 9, 9, 12), limit=1)
    assert slots[0].start == local(2026, 9, 10, 12, 15)


def test_slots_land_on_the_granularity_grid(scheduling_cfg):
    window = (local(2026, 9, 10, 0), local(2026, 9, 11, 0))
    busy = [(local(2026, 9, 10, 9), local(2026, 9, 10, 11, 50))]
    slots = find_slots(window, busy, 30, scheduling_cfg, TZ, now=utc(2026, 9, 9, 12), limit=1)
    # 11:50 + 15m buffer = 12:05, rounded up to the next :15 boundary.
    assert slots[0].start == local(2026, 9, 10, 12, 15)


def test_candidates_are_spread_across_days(scheduling_cfg):
    window = (local(2026, 9, 10, 0), local(2026, 9, 14, 0))
    slots = find_slots(window, [], 60, scheduling_cfg, TZ, now=utc(2026, 9, 9, 12), limit=3)
    days = {s.start.astimezone(TZ).date() for s in slots}
    assert len(slots) == 3
    assert len(days) == 3


def test_never_proposes_a_slot_in_the_past(scheduling_cfg):
    window = (local(2026, 9, 10, 0), local(2026, 9, 11, 0))
    now = local(2026, 9, 10, 15)  # 3pm Thursday
    slots = find_slots(window, [], 60, scheduling_cfg, TZ, now=now, limit=1)
    assert slots[0].start >= now


def test_fully_booked_day_yields_nothing(scheduling_cfg):
    window = (local(2026, 9, 10, 0), local(2026, 9, 11, 0))
    busy = [(local(2026, 9, 10, 0), local(2026, 9, 11, 0))]
    assert find_slots(window, busy, 60, scheduling_cfg, TZ, now=utc(2026, 9, 9, 12)) == []


def test_dst_transition_keeps_local_day_bounds(scheduling_cfg):
    """US DST ends 2026-11-01. The bookable day must stay 09:00-21:00 local."""
    window = (local(2026, 11, 1, 0), local(2026, 11, 2, 0))
    free = free_intervals(window, [], scheduling_cfg, TZ)
    assert len(free) == 1
    start_local = free[0][0].astimezone(TZ)
    end_local = free[0][1].astimezone(TZ)
    assert (start_local.hour, end_local.hour) == (9, 21)
    # 25-hour day: 09:00-21:00 local spans 12 wall-clock hours regardless.
    assert end_local - start_local == timedelta(hours=12)


def test_exact_time_outside_hours_is_flagged_not_moved(scheduling_cfg):
    slot = (local(2026, 9, 10, 22), local(2026, 9, 10, 23))
    assert violates_rules(slot, scheduling_cfg, TZ) == ["outside 09:00-21:00"]


def test_exact_time_over_protected_block_is_flagged(scheduling_cfg):
    slot = (local(2026, 9, 11, 19), local(2026, 9, 11, 20))  # Friday, jiu-jitsu
    reasons = violates_rules(slot, scheduling_cfg, TZ)
    assert reasons == ["overlaps protected block 'Jiu-jitsu'"]


def test_compliant_exact_time_has_no_complaints(scheduling_cfg):
    slot = (local(2026, 9, 10, 15), local(2026, 9, 10, 16))
    assert violates_rules(slot, scheduling_cfg, TZ) == []


def test_default_duration_prefers_the_longest_keyword(scheduling_cfg):
    scheduling_cfg.default_durations = {
        "lunch": 60, "meeting": 60, "standup": 15, "gym": 90, "_default": 45
    }
    assert scheduling_cfg.duration_for("Gym session") == 90
    assert scheduling_cfg.duration_for("Standup") == 15
    assert scheduling_cfg.duration_for("Coffee with Sam") == 45
