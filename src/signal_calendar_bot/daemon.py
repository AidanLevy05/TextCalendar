"""The daemon. Receive loop, gatekeeping, dispatch, and the confirmation fuse.

Order of operations for every inbound message, and the reason for each step:

1. **Allowlist.** Anything not from an allowed sender is dropped and logged.
   Nothing downstream ever sees it.
2. **Note to Self only.** v1 does not do group chats.
3. **Dedupe.** signal-cli redelivers on reconnect; the sqlite primary key is the
   lock. Without it one "add dentist 3pm" becomes two events.
4. **Heartbeat swallow.** Our own liveness pings never reach the model.
5. **Live proposal?** If this thread has one, the message is an answer to it —
   confirm, cancel, or (if the 60s window lapsed) discard. It is *never* both an
   answer and a new request.
6. **Otherwise** parse an intent and dispatch.
"""

from __future__ import annotations

import hashlib
import logging
import signal as signal_module
import threading
from datetime import UTC, datetime

from .config import Config
from .confirm import Action, ConfirmationManager
from .formatting import countdown_label
from .gcal.auth import ReauthRequired
from .gcal.client import CalendarClient
from .handlers import Handlers, Reply
from .heartbeat import Heartbeat
from .llm import ClaudeClient, LLMAuthError, LLMUnavailable
from .logging_setup import new_correlation_id, set_correlation_id
from .models import CreateIntent, DeleteIntent, MoveIntent, QueryIntent, UnknownIntent
from .signal_client import IncomingMessage, SignalClient, SignalError
from .store import Store

log = logging.getLogger(__name__)

RECONNECT_BACKOFF = (1, 2, 4, 8, 16, 30)
SWEEP_INTERVAL_SECONDS = 5


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.signal = SignalClient(cfg.signal)
        self.llm = ClaudeClient(cfg.anthropic)
        self.calendar = CalendarClient(cfg.google, cfg.general.tz)
        self.handlers = Handlers(cfg, self.calendar, self.llm)
        self.confirmations = ConfirmationManager(self.store, cfg.confirmation)
        self.heartbeat = Heartbeat(cfg.heartbeat, self.signal, self.store)
        self._stop = threading.Event()
        self._sweeper: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> int:
        self._install_signal_handlers()
        log.info(
            "starting",
            extra={
                "model": self.cfg.anthropic.model,
                "effort": self.cfg.anthropic.effort,
                "timezone": self.cfg.general.timezone,
                "confirm_ttl": self.cfg.confirmation.timeout_seconds,
            },
        )

        healthy, detail = self.llm.health()
        if not healthy:
            log.warning(
                "Claude API not reachable at startup; will retry per message",
                extra={"detail": detail},
            )

        self._start_sweeper()
        attempt = 0
        try:
            while not self._stop.is_set():
                try:
                    self.signal.connect()
                    self.signal.subscribe()
                    self.heartbeat.start()
                    attempt = 0
                    self._receive_forever()
                except (SignalError, OSError) as exc:
                    if self._stop.is_set():
                        break
                    delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
                    attempt += 1
                    log.error(
                        "signal connection lost, reconnecting",
                        extra={"error": str(exc), "retry_in": delay},
                    )
                    self.signal.close()
                    if self._stop.wait(delay):
                        break
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        self._stop.set()
        self.heartbeat.stop()
        self.signal.close()
        self.llm.close()
        self.store.close()
        log.info("stopped")

    def _install_signal_handlers(self) -> None:
        def handle(signum, _frame):
            log.info("received signal, shutting down", extra={"signal": signum})
            self._stop.set()
            self.signal.close()

        for sig in (signal_module.SIGTERM, signal_module.SIGINT):
            signal_module.signal(sig, handle)

    # -- receive loop ------------------------------------------------------

    def _receive_forever(self) -> None:
        log.info("listening")
        for message in self.signal.listen():
            if self._stop.is_set():
                return
            try:
                self.handle_message(message)
            except Exception:
                # One bad message must never take the daemon down.
                log.exception("unhandled error while processing message")
        log.warning("receive stream ended")

    # -- per-message pipeline ---------------------------------------------

    def handle_message(self, message: IncomingMessage) -> None:
        cid = new_correlation_id()

        if message.source not in self.cfg.signal.allowed_senders:
            log.warning(
                "dropped message from non-allowlisted sender",
                extra={"source": _mask(message.source)},
            )
            return

        if self.cfg.signal.note_to_self_only and not message.is_note_to_self(
            self.cfg.signal.account
        ):
            log.info("ignored non-note-to-self message", extra={"group": bool(message.group_id)})
            return

        body_hash = hashlib.sha256(message.body.encode("utf-8")).hexdigest()
        if not self.store.claim_message(
            message.message_key,
            source=_mask(message.source),
            timestamp_ms=message.timestamp,
            body_sha256=body_hash,
        ):
            log.info("duplicate message ignored", extra={"key": message.message_key})
            return

        if self.heartbeat.observe(message.body):
            return

        log.info(
            "message received",
            extra={"raw_text": message.body, "timestamp": message.timestamp},
        )

        thread_id = self.cfg.signal.account
        resolution = self.confirmations.resolve(thread_id, message.body)

        if resolution.outcome == "confirmed":
            self._execute_confirmed(resolution.pending, cid=cid)
            return

        if resolution.outcome == "expired":
            # The window closed before the reply landed. Nothing is written.
            self._reply(
                f"That expired — the {countdown_label(self.cfg.confirmation.timeout_seconds)} "
                "window closed. Nothing was changed. Send it again if you still want it."
            )
            return

        if resolution.outcome == "cancelled":
            from .confirm import Reply as ReplyKind
            from .confirm import classify_reply

            if classify_reply(message.body, self.cfg.confirmation) is ReplyKind.NEGATIVE:
                self._reply("Cancelled. Nothing was changed.")
                return
            # Anything else means they moved on — fall through and treat this
            # message as a fresh request.
            log.info("dropped stale proposal, treating message as a new request")

        self._dispatch(message.body, cid=cid)

    def _dispatch(self, text: str, *, cid: str) -> None:
        now = datetime.now(UTC)
        try:
            intent = self.llm.parse_intent(text, now=now, tz=self.cfg.general.tz)
        except LLMAuthError as exc:
            log.error("anthropic auth failure", extra={"error": str(exc)})
            self._reply(
                "My Anthropic API key is missing or rejected. Fix it on the host "
                "and I'll pick it up on the next message."
            )
            return
        except LLMUnavailable as exc:
            log.error("claude api unavailable", extra={"error": str(exc)})
            self._reply("Can't reach the Claude API right now. Try again in a moment.")
            return

        try:
            reply = self._route(intent, now=now)
        except ReauthRequired as exc:
            log.error("google reauth required", extra={"error": str(exc)})
            self._reply(
                "Google access needs re-authorizing. Run scripts/google_oauth_setup.py "
                "on the host."
            )
            return
        except Exception:
            log.exception("handler failed")
            self._reply("Something went wrong handling that. It's in the log.")
            return

        if reply.proposal is not None:
            self.confirmations.propose(
                self.cfg.signal.account,
                correlation_id=cid,
                action=reply.proposal.action,
                payload=reply.proposal.payload,
                preview_text=reply.proposal.preview,
            )
        self._reply(reply.text)

    def _route(self, intent, *, now: datetime) -> Reply:
        if isinstance(intent, QueryIntent):
            return self.handlers.handle_query(intent, now=now)
        if isinstance(intent, CreateIntent):
            return self.handlers.handle_create(intent, now=now)
        if isinstance(intent, MoveIntent):
            return self.handlers.handle_move(intent, now=now)
        if isinstance(intent, DeleteIntent):
            return self.handlers.handle_delete(intent, now=now)
        if isinstance(intent, UnknownIntent):
            return self.handlers.handle_unknown(intent)
        raise ValueError(f"unroutable intent {intent!r}")

    def _execute_confirmed(self, pending, *, cid: str) -> None:
        set_correlation_id(pending.correlation_id or cid)
        action = Action(pending.action)
        try:
            result_text = self.handlers.execute(
                action, pending.payload, correlation_id=pending.correlation_id
            )
        except ReauthRequired:
            log.exception("google reauth required during write")
            self._reply(
                "Google access expired before I could write that. "
                "Re-authorize on the host."
            )
            return
        except Exception:
            log.exception("write failed", extra={"action": pending.action})
            self._reply("That failed to write. Nothing was changed. Check the log.")
            return

        self.store.record_audit(
            correlation_id=pending.correlation_id,
            action=pending.action,
            event_id=pending.payload.get("event_id"),
            detail=pending.payload,
        )
        self._reply(result_text)

    # -- expiry sweeper ----------------------------------------------------

    def _start_sweeper(self) -> None:
        def loop() -> None:
            while not self._stop.wait(SWEEP_INTERVAL_SECONDS):
                try:
                    for pending in self.confirmations.sweep():
                        if not self.cfg.confirmation.notify_on_expiry:
                            continue
                        set_correlation_id(pending.correlation_id)
                        self._reply(
                            f"'{pending.preview_text}' expired after "
                            f"{countdown_label(self.cfg.confirmation.timeout_seconds)}. "
                            "Nothing was changed."
                        )
                except Exception:
                    log.exception("confirmation sweep failed")

        self._sweeper = threading.Thread(target=loop, name="expiry-sweeper", daemon=True)
        self._sweeper.start()

    # -- outbound ----------------------------------------------------------

    def _reply(self, text: str) -> None:
        try:
            self.signal.send_note_to_self(text)
        except SignalError as exc:
            log.error("failed to send reply", extra={"error": str(exc), "text": text[:200]})


def _mask(number: str) -> str:
    """Log phone numbers without writing them out in full."""
    if not number:
        return "(unknown)"
    return number[:3] + "…" + number[-2:] if len(number) > 5 else "…"


def run(cfg: Config) -> int:
    return Daemon(cfg).run()
