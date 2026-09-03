"""Structured logging.

Every message the bot handles gets a correlation id. For any given inbound text
you can grep one id out of the log and see: the raw text, the parsed intent, the
freebusy result, and the API call that was made.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import uuid
from contextvars import ContextVar
from typing import Any

from .config import LoggingConfig

# Correlation id for the message currently being handled.
_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")

_RESERVED = set(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


def new_correlation_id() -> str:
    cid = uuid.uuid4().hex[:12]
    _correlation_id.set(cid)
    return cid


def set_correlation_id(cid: str) -> None:
    _correlation_id.set(cid)


def current_correlation_id() -> str:
    return _correlation_id.get()


class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _correlation_id.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Extra kwargs land as top-level keys."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "cid": getattr(record, "correlation_id", "-"),
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key == "correlation_id":
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(cfg: LoggingConfig) -> None:
    root = logging.getLogger()
    root.setLevel(cfg.level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter: logging.Formatter = (
        JsonFormatter()
        if cfg.json_lines
        else logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(correlation_id)s] %(name)s: %(message)s"
        )
    )
    corr = _CorrelationFilter()

    cfg.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    file_handler = logging.handlers.RotatingFileHandler(
        cfg.path, maxBytes=cfg.max_bytes, backupCount=cfg.backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(corr)
    root.addHandler(file_handler)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    stream.addFilter(corr)
    root.addHandler(stream)

    # These are chatty and leak request URLs at DEBUG.
    for noisy in ("httpx", "httpcore", "googleapiclient.discovery_cache", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
