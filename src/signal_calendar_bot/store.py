"""Persistent state: message dedupe and pending confirmations.

Two jobs, both of which prevent a specific real-world failure:

* **Dedupe.** signal-cli redelivers on reconnect. Without persisted message ids,
  one "add dentist 3pm" silently becomes two events.
* **Confirmations.** A proposed write is parked here with a hard expiry. If the
  window lapses the row is destroyed, so a late "yes" has nothing to attach to.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Every Signal message we have already acted on.
CREATE TABLE IF NOT EXISTS processed_messages (
    message_key  TEXT PRIMARY KEY,
    source       TEXT,
    timestamp_ms INTEGER,
    body_sha256  TEXT,
    processed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processed_at ON processed_messages(processed_at);

-- At most one live proposal per conversation thread.
CREATE TABLE IF NOT EXISTS pending_confirmations (
    thread_id      TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    action         TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    preview_text   TEXT NOT NULL,
    created_at     REAL NOT NULL,
    expires_at     REAL NOT NULL
);

-- Append-only audit of everything the bot wrote to the calendar.
CREATE TABLE IF NOT EXISTS audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id TEXT,
    action         TEXT NOT NULL,
    event_id       TEXT,
    detail_json    TEXT,
    created_at     REAL NOT NULL
);

-- Outstanding heartbeat pings, cleared when the echo comes back.
CREATE TABLE IF NOT EXISTS heartbeats (
    nonce      TEXT PRIMARY KEY,
    sent_at    REAL NOT NULL,
    acked_at   REAL
);
"""


@dataclass(frozen=True)
class PendingConfirmation:
    thread_id: str
    correlation_id: str
    action: str
    payload: dict[str, Any]
    preview_text: str
    created_at: float
    expires_at: float

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, self.expires_at - time.time())

    def is_expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at


class Store:
    """Thread-safe sqlite wrapper. One instance per process."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()
        # The db can hold a refresh-token-adjacent audit trail; keep it private.
        try:
            self.path.chmod(0o600)
        except OSError:  # pragma: no cover - unusual filesystems
            pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- dedupe ------------------------------------------------------------

    def claim_message(
        self,
        message_key: str,
        *,
        source: str | None = None,
        timestamp_ms: int | None = None,
        body_sha256: str | None = None,
    ) -> bool:
        """Record a message id. Returns True if this is the first time we saw it.

        The insert itself is the lock: a redelivered message loses the race
        against the primary key and returns False.
        """
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO processed_messages"
                    " (message_key, source, timestamp_ms, body_sha256, processed_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (message_key, source, timestamp_ms, body_sha256, time.time()),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def prune_processed(self, older_than_days: int = 30) -> int:
        cutoff = time.time() - older_than_days * 86400
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM processed_messages WHERE processed_at < ?", (cutoff,)
            )
            self._conn.commit()
            return cur.rowcount

    # -- confirmations -----------------------------------------------------

    def put_pending(
        self,
        thread_id: str,
        *,
        correlation_id: str,
        action: str,
        payload: dict[str, Any],
        preview_text: str,
        ttl_seconds: int,
    ) -> PendingConfirmation:
        """Park a proposal. Replaces any existing proposal for the thread."""
        now = time.time()
        pending = PendingConfirmation(
            thread_id=thread_id,
            correlation_id=correlation_id,
            action=action,
            payload=payload,
            preview_text=preview_text,
            created_at=now,
            expires_at=now + ttl_seconds,
        )
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO pending_confirmations"
                " (thread_id, correlation_id, action, payload_json, preview_text,"
                "  created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    pending.thread_id,
                    pending.correlation_id,
                    pending.action,
                    json.dumps(pending.payload, default=str),
                    pending.preview_text,
                    pending.created_at,
                    pending.expires_at,
                ),
            )
            self._conn.commit()
        return pending

    def take_pending(self, thread_id: str) -> PendingConfirmation | None:
        """Atomically remove and return the thread's proposal, expired or not.

        Callers must check `is_expired()`. Removing it unconditionally is the
        point: one proposal, one chance to answer it.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pending_confirmations WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "DELETE FROM pending_confirmations WHERE thread_id = ?", (thread_id,)
            )
            self._conn.commit()
        return _row_to_pending(row)

    def peek_pending(self, thread_id: str) -> PendingConfirmation | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pending_confirmations WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return _row_to_pending(row) if row else None

    def drop_pending(self, thread_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM pending_confirmations WHERE thread_id = ?", (thread_id,)
            )
            self._conn.commit()

    def sweep_expired(self, now: float | None = None) -> list[PendingConfirmation]:
        """Remove every lapsed proposal and return them, so the daemon can say so."""
        now = now if now is not None else time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pending_confirmations WHERE expires_at <= ?", (now,)
            ).fetchall()
            if rows:
                self._conn.execute(
                    "DELETE FROM pending_confirmations WHERE expires_at <= ?", (now,)
                )
                self._conn.commit()
        return [_row_to_pending(r) for r in rows]

    # -- audit -------------------------------------------------------------

    def record_audit(
        self,
        *,
        correlation_id: str,
        action: str,
        event_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log (correlation_id, action, event_id, detail_json,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    correlation_id,
                    action,
                    event_id,
                    json.dumps(detail or {}, default=str),
                    time.time(),
                ),
            )
            self._conn.commit()

    # -- heartbeat ---------------------------------------------------------

    def record_heartbeat_sent(self, nonce: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO heartbeats (nonce, sent_at, acked_at)"
                " VALUES (?, ?, NULL)",
                (nonce, time.time()),
            )
            self._conn.commit()

    def ack_heartbeat(self, nonce: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE heartbeats SET acked_at = ? WHERE nonce = ? AND acked_at IS NULL",
                (time.time(), nonce),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def unacked_heartbeats(self, older_than_seconds: float) -> list[str]:
        cutoff = time.time() - older_than_seconds
        with self._lock:
            rows = self._conn.execute(
                "SELECT nonce FROM heartbeats WHERE acked_at IS NULL AND sent_at <= ?",
                (cutoff,),
            ).fetchall()
        return [r["nonce"] for r in rows]

    def clear_heartbeats(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM heartbeats")
            self._conn.commit()


def _row_to_pending(row: sqlite3.Row) -> PendingConfirmation:
    return PendingConfirmation(
        thread_id=row["thread_id"],
        correlation_id=row["correlation_id"],
        action=row["action"],
        payload=json.loads(row["payload_json"]),
        preview_text=row["preview_text"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def utc_now() -> datetime:
    return datetime.now(UTC)
