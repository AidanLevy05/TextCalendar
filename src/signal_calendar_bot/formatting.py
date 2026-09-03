"""Human-facing time formatting. Local timezone in, never UTC on screen."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def _strip_zero(value: str) -> str:
    return value.lstrip("0") or "0"


def clock(moment: datetime, tz: ZoneInfo) -> str:
    """'3pm' / '3:30pm'"""
    local = moment.astimezone(tz)
    hour = _strip_zero(local.strftime("%I"))
    suffix = local.strftime("%p").lower()
    if local.minute:
        return f"{hour}:{local.strftime('%M')}{suffix}"
    return f"{hour}{suffix}"


def day_label(moment: datetime, tz: ZoneInfo, *, now: datetime | None = None) -> str:
    """'today' / 'tomorrow' / 'Thursday' / 'Thu Sep 25'"""
    local = moment.astimezone(tz)
    now_local = (now or datetime.now(tz)).astimezone(tz)
    delta_days = (local.date() - now_local.date()).days
    if delta_days == 0:
        return "today"
    if delta_days == 1:
        return "tomorrow"
    if 2 <= delta_days <= 6:
        return local.strftime("%A")
    return local.strftime("%a %b ") + _strip_zero(local.strftime("%d"))


def slot_label(start: datetime, end: datetime, tz: ZoneInfo, *, now: datetime | None = None) -> str:
    """'Thursday 12pm-1pm'"""
    return f"{day_label(start, tz, now=now)} {clock(start, tz)}-{clock(end, tz)}"


def duration_label(minutes: int) -> str:
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours}h{mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def countdown_label(seconds: int) -> str:
    if seconds % 60 == 0 and seconds >= 60:
        minutes = seconds // 60
        return f"{minutes} minute" + ("s" if minutes != 1 else "")
    return f"{seconds} seconds"


def relative_window(start: datetime, end: datetime, tz: ZoneInfo) -> str:
    span = end - start
    if span >= timedelta(days=1):
        return f"{day_label(start, tz)} through {day_label(end - timedelta(seconds=1), tz)}"
    return slot_label(start, end, tz)
