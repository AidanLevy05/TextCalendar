"""signal-cli JSON-RPC client.

signal-cli runs in `daemon` mode as a linked secondary device on the user's
existing account. This talks to it over a unix socket (default) or TCP, reading
newline-delimited JSON-RPC.

The important detail is the receive loop: signal-cli's websocket to the Signal
service can die quietly while the process stays alive, so the socket looks
healthy and simply delivers nothing forever. This class only reports what it
sees; `heartbeat.py` is what proves the link is actually live.
"""

from __future__ import annotations

import itertools
import json
import logging
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from .config import SignalConfig

log = logging.getLogger(__name__)

RECV_BUFFER = 65536


class SignalError(RuntimeError):
    """signal-cli returned a JSON-RPC error, or the transport failed."""


@dataclass(frozen=True)
class IncomingMessage:
    """One inbound Signal message, normalized."""

    source: str
    source_uuid: str | None
    destination: str | None
    timestamp: int
    body: str
    group_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def message_key(self) -> str:
        """Stable id for dedupe. signal-cli redelivers on reconnect."""
        return f"{self.source}:{self.timestamp}"

    def is_note_to_self(self, account: str) -> bool:
        """Note to Self is a message from yourself addressed to yourself."""
        if self.group_id:
            return False
        return self.source == account and (
            self.destination is None or self.destination == account
        )


class SignalClient:
    """Blocking JSON-RPC client. One connection, guarded by a send lock."""

    def __init__(self, cfg: SignalConfig) -> None:
        self.cfg = cfg
        self._sock: socket.socket | None = None
        self._buffer = b""
        self._send_lock = threading.Lock()
        self._ids = itertools.count(1)
        # Responses that arrive interleaved with notifications, keyed by id.
        self._responses: dict[int, dict[str, Any]] = {}

    # -- connection --------------------------------------------------------

    def connect(self) -> None:
        if self._sock is not None:
            return
        if self.cfg.transport == "unix":
            path = self.cfg.socket
            if not path.exists():
                raise SignalError(
                    f"signal-cli socket not found at {path}. Is signal-cli running in "
                    "daemon mode? Try: systemctl --user status signal-cli.service"
                )
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(path))
        else:
            sock = socket.create_connection((self.cfg.host, self.cfg.port), timeout=10)
        sock.settimeout(None)
        self._sock = sock
        self._buffer = b""
        log.info(
            "connected to signal-cli",
            extra={"transport": self.cfg.transport},
        )

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._buffer = b""

    def reconnect(self) -> None:
        self.close()
        self.connect()

    # -- framing -----------------------------------------------------------

    def _read_line(self) -> dict[str, Any] | None:
        """Read one newline-delimited JSON object. None means the peer closed."""
        if self._sock is None:
            raise SignalError("not connected")
        while b"\n" not in self._buffer:
            chunk = self._sock.recv(RECV_BUFFER)
            if not chunk:
                log.warning("signal-cli closed the connection")
                return None
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        line = line.strip()
        if not line:
            return {}
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            log.warning(
                "unparseable line from signal-cli",
                extra={"line": line[:400].decode("utf-8", "replace")},
            )
            return {}

    def _send(self, method: str, params: dict[str, Any] | None = None) -> int:
        if self._sock is None:
            raise SignalError("not connected")
        request_id = next(self._ids)
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "id": request_id,
            "params": params or {},
        }
        data = (json.dumps(payload) + "\n").encode("utf-8")
        with self._send_lock:
            self._sock.sendall(data)
        return request_id

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0
    ) -> Any:
        """Send a request and read until its response arrives.

        Notifications seen while waiting are stashed so the receive loop is not
        the only place messages can surface.
        """
        request_id = self._send(method, params)
        if request_id in self._responses:
            return self._take_response(request_id)
        if self._sock is not None:
            self._sock.settimeout(timeout)
        try:
            while True:
                message = self._read_line()
                if message is None:
                    raise SignalError("connection closed while awaiting response")
                if message.get("id") == request_id:
                    self._responses[request_id] = message
                    return self._take_response(request_id)
                if "id" in message and message.get("id") is not None:
                    self._responses[message["id"]] = message
        except TimeoutError as exc:
            raise SignalError(f"timed out calling {method}") from exc
        finally:
            if self._sock is not None:
                self._sock.settimeout(None)

    def _take_response(self, request_id: int) -> Any:
        message = self._responses.pop(request_id)
        if "error" in message:
            raise SignalError(f"signal-cli error: {message['error']}")
        return message.get("result")

    # -- sending -----------------------------------------------------------

    def send_note_to_self(self, text: str) -> int | None:
        """Send a message to the user's own Note to Self thread."""
        result = self.call(
            "send",
            {"account": self.cfg.account, "recipient": [self.cfg.account], "message": text},
        )
        timestamp = (result or {}).get("timestamp") if isinstance(result, dict) else None
        log.info("sent note-to-self", extra={"chars": len(text), "timestamp": timestamp})
        return timestamp

    def send_to(self, recipient: str, text: str) -> int | None:
        result = self.call(
            "send",
            {"account": self.cfg.account, "recipient": [recipient], "message": text},
        )
        return (result or {}).get("timestamp") if isinstance(result, dict) else None

    # -- receiving ---------------------------------------------------------

    def subscribe(self) -> None:
        """Ask signal-cli to stream incoming envelopes on this connection."""
        try:
            self.call("subscribeReceive", {"account": self.cfg.account}, timeout=15.0)
            log.info("subscribed to receive stream")
        except SignalError as exc:
            # Older signal-cli builds stream unsolicited without an explicit
            # subscribe; that is not a fatal condition.
            log.info("subscribeReceive unavailable, relying on unsolicited stream",
                     extra={"detail": str(exc)[:200]})

    def listen(self) -> Iterator[IncomingMessage]:
        """Yield inbound messages until the connection drops.

        Returns (rather than raising) on a clean close so the caller can decide
        the reconnect policy.
        """
        while True:
            message = self._read_line()
            if message is None:
                return
            if not message:
                continue
            if "id" in message and message.get("id") is not None and "method" not in message:
                self._responses[message["id"]] = message
                continue
            parsed = self._parse_envelope(message)
            if parsed is not None:
                yield parsed

    @staticmethod
    def _parse_envelope(message: dict[str, Any]) -> IncomingMessage | None:
        """Pull a text message out of a signal-cli `receive` notification."""
        if message.get("method") != "receive":
            return None
        params = message.get("params") or {}
        envelope = params.get("envelope") or {}

        # A message you send from your phone comes back as syncMessage/sentMessage;
        # that is exactly how Note to Self arrives at a linked device.
        data = envelope.get("dataMessage")
        destination = None
        if data is None:
            sync = (envelope.get("syncMessage") or {}).get("sentMessage")
            if sync is None:
                return None
            data = sync
            destination = sync.get("destinationNumber") or sync.get("destination")

        body = data.get("message")
        if not body:
            return None  # reactions, receipts, typing indicators, attachments-only

        group_info = data.get("groupInfo") or {}
        return IncomingMessage(
            source=envelope.get("sourceNumber") or envelope.get("source") or "",
            source_uuid=envelope.get("sourceUuid"),
            destination=destination,
            timestamp=int(envelope.get("timestamp") or data.get("timestamp") or 0),
            body=body,
            group_id=group_info.get("groupId"),
            raw=message,
        )
