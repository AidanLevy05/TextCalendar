"""The model's entire output surface.

Core design rule: the model does language, code does logic. The only thing the
LLM is allowed to emit is one of these objects. It never does date arithmetic,
never decides whether two events conflict, and never picks a slot — it hands
over a window and code takes it from there.

Keep this schema small. A 4B-8B model degrades fast as the surface grows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_TITLE_LEN = 200
MAX_DURATION_MINUTES = 12 * 60
MAX_WINDOW_DAYS = 60


class Intent(str, Enum):
    CREATE = "create"
    QUERY = "query"
    MOVE = "move"
    DELETE = "delete"
    UNKNOWN = "unknown"


WRITE_INTENTS = {Intent.CREATE, Intent.MOVE, Intent.DELETE}


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _as_utc(value: datetime) -> datetime:
    """Reject naive datetimes; everything internal is UTC-aware."""
    if value.tzinfo is None:
        raise ValueError("datetime must include a timezone offset")
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, Field(description="ISO-8601 with offset")]


class WindowMixin(_Base):
    window_start: UtcDatetime
    window_end: UtcDatetime

    @field_validator("window_start", "window_end")
    @classmethod
    def _must_be_aware(cls, v: datetime) -> datetime:
        return _as_utc(v)

    @model_validator(mode="after")
    def _window_sane(self):
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        if self.window_end - self.window_start > timedelta(days=MAX_WINDOW_DAYS):
            raise ValueError(f"search window longer than {MAX_WINDOW_DAYS} days")
        return self


class CreateIntent(WindowMixin):
    intent: Literal[Intent.CREATE] = Intent.CREATE
    title: str = Field(min_length=1, max_length=MAX_TITLE_LEN)
    duration_minutes: int | None = Field(default=None, ge=5, le=MAX_DURATION_MINUTES)
    location: str | None = Field(default=None, max_length=MAX_TITLE_LEN)
    # Names only — v1 does not send invitations, this is for the title/description.
    attendees: list[str] = Field(default_factory=list, max_length=10)
    # True when the user named an exact time ("dentist at 3pm Tuesday") rather
    # than asking the bot to find a slot ("lunch with Dan Thursday").
    exact_time: bool = False


class QueryIntent(WindowMixin):
    intent: Literal[Intent.QUERY] = Intent.QUERY
    # Free-text description of what was asked, echoed back for phrasing.
    subject: str | None = Field(default=None, max_length=MAX_TITLE_LEN)


class MoveIntent(WindowMixin):
    intent: Literal[Intent.MOVE] = Intent.MOVE
    # Text used to find the existing event; code does the matching, not the model.
    target_query: str = Field(min_length=1, max_length=MAX_TITLE_LEN)
    # Window to search for the *existing* event. Defaults to the next 14 days.
    target_window_start: UtcDatetime | None = None
    target_window_end: UtcDatetime | None = None
    duration_minutes: int | None = Field(default=None, ge=5, le=MAX_DURATION_MINUTES)
    exact_time: bool = False

    @field_validator("target_window_start", "target_window_end")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        return None if v is None else _as_utc(v)


class DeleteIntent(WindowMixin):
    intent: Literal[Intent.DELETE] = Intent.DELETE
    target_query: str = Field(min_length=1, max_length=MAX_TITLE_LEN)


class UnknownIntent(_Base):
    intent: Literal[Intent.UNKNOWN] = Intent.UNKNOWN
    # What the model could not resolve, used to phrase the clarifying question.
    reason: str | None = Field(default=None, max_length=MAX_TITLE_LEN)


ParsedIntent = Annotated[
    CreateIntent | QueryIntent | MoveIntent | DeleteIntent | UnknownIntent,
    Field(discriminator="intent"),
]


class IntentEnvelope(_Base):
    """Wrapper pydantic validates the raw model JSON against."""

    parsed: ParsedIntent


def validate_intent(
    raw: dict,
) -> CreateIntent | QueryIntent | MoveIntent | DeleteIntent | UnknownIntent:
    """Validate a raw dict from the model. Raises pydantic.ValidationError."""
    return IntentEnvelope.model_validate({"parsed": raw}).parsed


# --------------------------------------------------------------------------
# Internal (non-model) types
# --------------------------------------------------------------------------


class TimeSlot(_Base):
    start: datetime
    end: datetime

    @property
    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


class CalendarEvent(_Base):
    """Normalized view of a Google Calendar event."""

    id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str | None = None
    created_by_bot: bool = False
    html_link: str | None = None
