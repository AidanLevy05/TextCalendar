"""The write-confirmation window.

Reads execute immediately. Writes never do — the bot states exactly what it will
do and waits. The proposal lives for `confirmation.timeout_seconds` (60 by
default) and then it is *destroyed*: no write happens, and the original request
has to be sent again.

That hard expiry is the whole point. A hallucinated delete on a real calendar is
the failure mode that kills this project, and the second-worst version of it is
a "yes" typed twenty minutes later landing on a proposal the user has stopped
thinking about. There is no grace period and no "did you mean the earlier one" —
a lapsed proposal is gone.
"""

from __future__ import annotations

import logging
import re
import string
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .config import ConfirmationConfig
from .store import PendingConfirmation, Store

log = logging.getLogger(__name__)


class Action(str, Enum):
    """What a parked proposal will do if confirmed."""

    CREATE = "create"
    MOVE = "move"
    DELETE = "delete"


class Reply(str, Enum):
    AFFIRMATIVE = "affirmative"
    NEGATIVE = "negative"
    UNRELATED = "unrelated"


@dataclass(frozen=True)
class Resolution:
    """Outcome of feeding a message to a thread that had a live proposal."""

    outcome: str  # "confirmed" | "cancelled" | "expired" | "none"
    pending: PendingConfirmation | None = None

    @property
    def confirmed(self) -> bool:
        return self.outcome == "confirmed"


_PUNCT = str.maketrans("", "", string.punctuation.replace("+", ""))
_WHITESPACE = re.compile(r"\s+")


def normalize_reply(text: str) -> str:
    return _WHITESPACE.sub(" ", text.translate(_PUNCT).strip().lower())


def classify_reply(text: str, cfg: ConfirmationConfig) -> Reply:
    """Only an exact match counts.

    Deliberately strict: "no, not thursday" must not be read as an affirmative
    because it happens to contain a word, and a substring match on "y" would
    confirm on almost anything. Anything unrecognized is UNRELATED, which
    cancels the proposal rather than guessing.
    """
    normalized = normalize_reply(text)
    if not normalized:
        return Reply.UNRELATED
    if normalized in {normalize_reply(a) for a in cfg.affirmatives}:
        return Reply.AFFIRMATIVE
    if normalized in {normalize_reply(n) for n in cfg.negatives}:
        return Reply.NEGATIVE
    return Reply.UNRELATED


class ConfirmationManager:
    """Owns the lifecycle of pending writes."""

    def __init__(self, store: Store, cfg: ConfirmationConfig) -> None:
        self.store = store
        self.cfg = cfg

    @property
    def ttl(self) -> int:
        return self.cfg.timeout_seconds

    def propose(
        self,
        thread_id: str,
        *,
        correlation_id: str,
        action: Action,
        payload: dict[str, Any],
        preview_text: str,
    ) -> PendingConfirmation:
        """Park a write. Any earlier proposal on this thread is replaced."""
        existing = self.store.peek_pending(thread_id)
        if existing is not None:
            log.info(
                "replacing live proposal",
                extra={"thread_id": thread_id, "replaced_action": existing.action},
            )
        pending = self.store.put_pending(
            thread_id,
            correlation_id=correlation_id,
            action=action.value,
            payload=payload,
            preview_text=preview_text,
            ttl_seconds=self.ttl,
        )
        log.info(
            "proposal parked",
            extra={
                "thread_id": thread_id,
                "action": action.value,
                "ttl_seconds": self.ttl,
                "expires_at": pending.expires_at,
            },
        )
        return pending

    def resolve(self, thread_id: str, message: str) -> Resolution:
        """Apply an inbound message to this thread's proposal, if any.

        The proposal is removed from the store *unconditionally* the moment it is
        looked at. There is exactly one chance to answer it: confirm, cancel, or
        let it lapse.
        """
        pending = self.store.take_pending(thread_id)
        if pending is None:
            return Resolution(outcome="none")

        if pending.is_expired():
            log.info(
                "proposal had already expired when the reply arrived",
                extra={
                    "thread_id": thread_id,
                    "action": pending.action,
                    "late_by_seconds": round(time.time() - pending.expires_at, 1),
                },
            )
            return Resolution(outcome="expired", pending=pending)

        reply = classify_reply(message, self.cfg)
        if reply is Reply.AFFIRMATIVE:
            log.info(
                "proposal confirmed",
                extra={"thread_id": thread_id, "action": pending.action},
            )
            return Resolution(outcome="confirmed", pending=pending)

        if reply is Reply.NEGATIVE:
            log.info(
                "proposal cancelled",
                extra={"thread_id": thread_id, "action": pending.action},
            )
            return Resolution(outcome="cancelled", pending=pending)

        # Anything else means the user moved on. Drop the proposal and let the
        # message be handled as a fresh request; never guess at consent.
        log.info(
            "proposal dropped, reply was a new request",
            extra={"thread_id": thread_id, "action": pending.action},
        )
        return Resolution(outcome="cancelled", pending=pending)

    def sweep(self) -> list[PendingConfirmation]:
        """Reap proposals whose window has closed. Called on a timer."""
        expired = self.store.sweep_expired()
        for pending in expired:
            log.info(
                "proposal expired unanswered",
                extra={
                    "thread_id": pending.thread_id,
                    "action": pending.action,
                    "ttl_seconds": self.ttl,
                },
            )
        return expired
