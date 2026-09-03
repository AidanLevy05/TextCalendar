"""Liveness proof for the signal-cli link.

The failure this exists for: signal-cli's websocket to the Signal service can
die while the process stays up. The socket accepts connections, the unit shows
active, `send` may even succeed — and nothing is ever received again. Nothing
about the process's own state reveals this.

So the daemon proves the loop end-to-end instead: it sends itself a nonce via
Note to Self and expects that exact nonce to come back through the receive path.
Miss `max_missed` in a row and the signal-cli unit is restarted.
"""

from __future__ import annotations

import logging
import re
import secrets
import shlex
import subprocess
import threading
import time

from .config import HeartbeatConfig
from .signal_client import SignalClient, SignalError
from .store import Store

log = logging.getLogger(__name__)

PING_PREFIX = "​[hb]"  # zero-width space keeps it visually quiet in the thread
_PING_RE = re.compile(re.escape(PING_PREFIX) + r"\s*([0-9a-f]{16})")


def is_heartbeat(text: str) -> str | None:
    """Return the nonce if this message is one of our pings."""
    match = _PING_RE.search(text or "")
    return match.group(1) if match else None


class Heartbeat:
    def __init__(
        self,
        cfg: HeartbeatConfig,
        signal: SignalClient,
        store: Store,
        *,
        on_dead=None,
    ) -> None:
        self.cfg = cfg
        self.signal = signal
        self.store = store
        self.on_dead = on_dead
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._missed = 0

    def start(self) -> None:
        if not self.cfg.enabled:
            log.info("heartbeat disabled")
            return
        self.store.clear_heartbeats()
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
        self._thread.start()
        log.info("heartbeat started", extra={"interval_seconds": self.cfg.interval_seconds})

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def observe(self, text: str) -> bool:
        """Feed every inbound message here. True means it was a ping (swallow it)."""
        nonce = is_heartbeat(text)
        if nonce is None:
            return False
        if self.store.ack_heartbeat(nonce):
            self._missed = 0
            log.debug("heartbeat acked", extra={"nonce": nonce})
        return True

    def _run(self) -> None:
        # Stagger the first ping so startup isn't noisy.
        if self._stop.wait(min(30, self.cfg.interval_seconds)):
            return
        while not self._stop.is_set():
            self._cycle()
            if self._stop.wait(self.cfg.interval_seconds):
                return

    def _cycle(self) -> None:
        nonce = secrets.token_hex(8)
        self.store.record_heartbeat_sent(nonce)
        try:
            self.signal.send_note_to_self(f"{PING_PREFIX} {nonce}")
        except SignalError as exc:
            log.error("heartbeat send failed", extra={"error": str(exc)})
            self._register_miss("send failed")
            return

        deadline = time.time() + self.cfg.timeout_seconds
        while time.time() < deadline:
            if self._stop.wait(1.0):
                return
            if nonce not in self.store.unacked_heartbeats(0):
                return  # the ping came back through the receive path

        self._register_miss("no echo")

    def _register_miss(self, reason: str) -> None:
        self._missed += 1
        log.warning(
            "heartbeat missed",
            extra={"reason": reason, "consecutive": self._missed, "max": self.cfg.max_missed},
        )
        if self._missed < self.cfg.max_missed:
            return

        log.error("signal link presumed dead", extra={"missed": self._missed})
        self._missed = 0
        if self.on_dead is not None:
            try:
                self.on_dead()
            except Exception:  # pragma: no cover - callback is best-effort
                log.exception("on_dead callback failed")

        command = self.cfg.restart_command.strip()
        if not command:
            return
        try:
            subprocess.run(shlex.split(command), check=False, timeout=30)
            log.info("ran restart command", extra={"command": command})
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("restart command failed", extra={"error": str(exc)})
