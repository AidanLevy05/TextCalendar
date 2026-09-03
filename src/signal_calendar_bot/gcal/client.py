"""Google Calendar client.

Narrow on purpose. Two rules hold everywhere in this file:

* **freebusy, not events.list, for availability.** Smaller payload, and no event
  details from other people's meetings enter the process at all.
* **Every event this bot creates is tagged** with
  `extendedProperties.private.source = "signal-bot"`. That gives a one-command
  purge of everything it ever created plus an audit trail. It is applied in
  `create_event` itself so there is no path that writes an untagged event.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ..config import GoogleConfig
from ..models import CalendarEvent
from ..sanitize import looks_like_injection
from .auth import ReauthRequired, get_credentials

log = logging.getLogger(__name__)

BOT_SOURCE_TAG = "signal-bot"
SOURCE_KEY = "source"

Interval = tuple[datetime, datetime]


def _rfc3339(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("naive datetime reached the Calendar API layer")
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_event_time(node: dict[str, Any], tz: ZoneInfo) -> tuple[datetime, bool]:
    """Google gives either dateTime (timed) or date (all-day)."""
    if "dateTime" in node:
        return datetime.fromisoformat(node["dateTime"]).astimezone(UTC), False
    day = datetime.fromisoformat(node["date"])
    return day.replace(tzinfo=tz).astimezone(UTC), True


class CalendarClient:
    """Thin wrapper over the Calendar v3 API with the project's invariants baked in."""

    def __init__(self, cfg: GoogleConfig, tz: ZoneInfo) -> None:
        self.cfg = cfg
        self.tz = tz
        self._service = None

    @property
    def service(self):
        if self._service is None:
            creds = get_credentials(self.cfg, allow_interactive=False)
            self._service = build(
                "calendar", "v3", credentials=creds, cache_discovery=False
            )
        return self._service

    def reset(self) -> None:
        """Drop the cached service so the next call re-reads credentials."""
        self._service = None

    # -- availability ------------------------------------------------------

    def freebusy(self, window: Interval) -> list[Interval]:
        """Busy intervals in the window. No event details are returned."""
        start, end = window
        body = {
            "timeMin": _rfc3339(start),
            "timeMax": _rfc3339(end),
            "timeZone": "UTC",
            "items": [{"id": self.cfg.calendar_id}],
        }
        response = self._execute(self.service.freebusy().query(body=body))
        cal = response.get("calendars", {}).get(self.cfg.calendar_id, {})
        if cal.get("errors"):
            raise RuntimeError(f"freebusy error: {cal['errors']}")
        busy = [
            (
                datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                datetime.fromisoformat(b["end"].replace("Z", "+00:00")),
            )
            for b in cal.get("busy", [])
        ]
        log.info(
            "freebusy",
            extra={
                "window_start": _rfc3339(start),
                "window_end": _rfc3339(end),
                "busy_count": len(busy),
            },
        )
        return busy

    # -- reads -------------------------------------------------------------

    def list_events(self, window: Interval, *, max_results: int = 50) -> list[CalendarEvent]:
        """Events in a window. Used for `query`, and to resolve move/delete targets.

        Titles that come back are untrusted; callers must scrub before prompting.
        """
        start, end = window
        response = self._execute(
            self.service.events().list(
                calendarId=self.cfg.calendar_id,
                timeMin=_rfc3339(start),
                timeMax=_rfc3339(end),
                singleEvents=True,
                orderBy="startTime",
                maxResults=max_results,
            )
        )
        events: list[CalendarEvent] = []
        for item in response.get("items", []):
            if item.get("status") == "cancelled":
                continue
            raw_title = item.get("summary", "")
            if looks_like_injection(raw_title) or looks_like_injection(item.get("location")):
                log.warning(
                    "calendar text contains prompt-control phrasing; will be scrubbed",
                    extra={"event_id": item.get("id")},
                )
            ev_start, all_day = _parse_event_time(item["start"], self.tz)
            ev_end, _ = _parse_event_time(item["end"], self.tz)
            props = item.get("extendedProperties", {}).get("private", {})
            events.append(
                CalendarEvent(
                    id=item["id"],
                    title=raw_title or "(untitled)",
                    start=ev_start,
                    end=ev_end,
                    all_day=all_day,
                    location=item.get("location"),
                    created_by_bot=props.get(SOURCE_KEY) == BOT_SOURCE_TAG,
                    html_link=item.get("htmlLink"),
                )
            )
        log.info("events.list", extra={"count": len(events)})
        return events

    def get_event(self, event_id: str) -> CalendarEvent | None:
        try:
            item = self._execute(
                self.service.events().get(
                    calendarId=self.cfg.calendar_id, eventId=event_id
                )
            )
        except HttpError as exc:
            if exc.resp.status == 404:
                return None
            raise
        ev_start, all_day = _parse_event_time(item["start"], self.tz)
        ev_end, _ = _parse_event_time(item["end"], self.tz)
        props = item.get("extendedProperties", {}).get("private", {})
        return CalendarEvent(
            id=item["id"],
            title=item.get("summary", "(untitled)"),
            start=ev_start,
            end=ev_end,
            all_day=all_day,
            location=item.get("location"),
            created_by_bot=props.get(SOURCE_KEY) == BOT_SOURCE_TAG,
            html_link=item.get("htmlLink"),
        )

    def find_bot_events(self, window: Interval | None = None) -> list[CalendarEvent]:
        """Every event this bot created. Backs the one-command purge."""
        if window is None:
            now = datetime.now(UTC)
            window = (now - timedelta(days=365), now + timedelta(days=365))
        response = self._execute(
            self.service.events().list(
                calendarId=self.cfg.calendar_id,
                timeMin=_rfc3339(window[0]),
                timeMax=_rfc3339(window[1]),
                singleEvents=True,
                orderBy="startTime",
                maxResults=2500,
                privateExtendedProperty=f"{SOURCE_KEY}={BOT_SOURCE_TAG}",
            )
        )
        out: list[CalendarEvent] = []
        for item in response.get("items", []):
            ev_start, all_day = _parse_event_time(item["start"], self.tz)
            ev_end, _ = _parse_event_time(item["end"], self.tz)
            out.append(
                CalendarEvent(
                    id=item["id"],
                    title=item.get("summary", "(untitled)"),
                    start=ev_start,
                    end=ev_end,
                    all_day=all_day,
                    location=item.get("location"),
                    created_by_bot=True,
                    html_link=item.get("htmlLink"),
                )
            )
        return out

    # -- writes ------------------------------------------------------------

    def create_event(
        self,
        *,
        title: str,
        start: datetime,
        end: datetime,
        location: str | None = None,
        description: str | None = None,
        correlation_id: str | None = None,
    ) -> CalendarEvent:
        """Create a tagged event. This is the only create path in the codebase."""
        body: dict[str, Any] = {
            "summary": title,
            "start": {"dateTime": _rfc3339(start), "timeZone": "UTC"},
            "end": {"dateTime": _rfc3339(end), "timeZone": "UTC"},
            "extendedProperties": {
                "private": {
                    SOURCE_KEY: BOT_SOURCE_TAG,
                    "created_at": _rfc3339(datetime.now(UTC)),
                    **({"correlation_id": correlation_id} if correlation_id else {}),
                }
            },
        }
        if location:
            body["location"] = location
        if description:
            body["description"] = description

        item = self._execute(
            self.service.events().insert(calendarId=self.cfg.calendar_id, body=body)
        )
        log.info(
            "events.insert",
            extra={"event_id": item["id"], "start": _rfc3339(start), "end": _rfc3339(end)},
        )
        return CalendarEvent(
            id=item["id"],
            title=item.get("summary", title),
            start=start.astimezone(UTC),
            end=end.astimezone(UTC),
            location=item.get("location"),
            created_by_bot=True,
            html_link=item.get("htmlLink"),
        )

    def move_event(self, event_id: str, *, start: datetime, end: datetime) -> CalendarEvent:
        """Reschedule an event, preserving (and re-asserting) the bot tag if present."""
        existing = self._execute(
            self.service.events().get(calendarId=self.cfg.calendar_id, eventId=event_id)
        )
        private = existing.get("extendedProperties", {}).get("private", {})
        private["moved_by"] = BOT_SOURCE_TAG
        private["moved_at"] = _rfc3339(datetime.now(UTC))
        body = {
            "start": {"dateTime": _rfc3339(start), "timeZone": "UTC"},
            "end": {"dateTime": _rfc3339(end), "timeZone": "UTC"},
            "extendedProperties": {"private": private},
        }
        item = self._execute(
            self.service.events().patch(
                calendarId=self.cfg.calendar_id, eventId=event_id, body=body
            )
        )
        log.info("events.patch", extra={"event_id": event_id, "start": _rfc3339(start)})
        return CalendarEvent(
            id=item["id"],
            title=item.get("summary", "(untitled)"),
            start=start.astimezone(UTC),
            end=end.astimezone(UTC),
            location=item.get("location"),
            created_by_bot=private.get(SOURCE_KEY) == BOT_SOURCE_TAG,
            html_link=item.get("htmlLink"),
        )

    def delete_event(self, event_id: str) -> None:
        self._execute(
            self.service.events().delete(
                calendarId=self.cfg.calendar_id, eventId=event_id
            )
        )
        log.info("events.delete", extra={"event_id": event_id})

    # -- plumbing ----------------------------------------------------------

    def _execute(self, request):
        """Run a request, converting auth failures into ReauthRequired."""
        try:
            return request.execute()
        except HttpError as exc:
            if exc.resp.status in (401, 403) and b"invalid_grant" in (exc.content or b""):
                self.reset()
                raise ReauthRequired(
                    "Google rejected the stored credentials. "
                    "Re-run: python scripts/google_oauth_setup.py"
                ) from exc
            log.error(
                "calendar api error",
                extra={"status": exc.resp.status, "detail": str(exc)[:500]},
            )
            raise
