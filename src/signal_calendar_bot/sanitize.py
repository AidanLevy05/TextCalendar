"""Prompt-injection defense for calendar-sourced text.

Event titles and descriptions come from invites the user did not write. They are
untrusted input and must never be interpretable as instructions. Everything that
travels calendar -> prompt goes through `untrusted()` first.
"""

from __future__ import annotations

import re
import unicodedata

MAX_FIELD_LEN = 160

# Zero-width and bidi-control characters, the classic way to smuggle text past
# a human reading the log while the model still sees it.
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁯﻿]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")

# Phrasings that only ever appear in an injection attempt inside an event title.
_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?", re.I),
    re.compile(r"disregard\s+(all\s+)?(previous|prior|the\s+above)", re.I),
    re.compile(r"\bsystem\s*prompt\b", re.I),
    re.compile(r"\byou\s+are\s+now\b", re.I),
    re.compile(r"\bnew\s+instructions?\b", re.I),
    re.compile(r"<\s*/?\s*(system|assistant|user|untrusted[_-]?\w*)\s*>", re.I),
    re.compile(r"\[/?INST\]", re.I),
    re.compile(r"<\|.*?\|>"),
)

REDACTION = "[redacted]"


def scrub(text: str | None, *, max_len: int = MAX_FIELD_LEN) -> str:
    """Normalize and neutralize a single untrusted field.

    Removes invisible/control characters, collapses whitespace, strips anything
    that looks like a prompt-control token, and truncates.
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", str(text))
    out = _INVISIBLE.sub("", out)
    out = _CONTROL.sub(" ", out)
    for pattern in _INJECTION_PATTERNS:
        out = pattern.sub(REDACTION, out)
    # Neutralize delimiter characters so a field cannot close our fence.
    out = out.replace("<", "‹").replace(">", "›")
    out = out.replace("```", "'''")
    out = _WHITESPACE.sub(" ", out).strip()
    if len(out) > max_len:
        out = out[: max_len - 1].rstrip() + "…"
    return out


def untrusted(text: str | None, *, label: str = "untrusted") -> str:
    """Wrap a scrubbed field in an explicit data fence for the prompt."""
    return f"<{label}>{scrub(text)}</{label}>"


def looks_like_injection(text: str | None) -> bool:
    """True when the raw text contained a prompt-control phrasing. For logging."""
    if not text:
        return False
    normalized = _INVISIBLE.sub("", unicodedata.normalize("NFKC", str(text)))
    return any(p.search(normalized) for p in _INJECTION_PATTERNS)


def scrub_event_for_prompt(title: str | None, location: str | None = None) -> dict[str, str]:
    """Scrub the only two calendar fields v1 ever shows the model."""
    return {"title": scrub(title) or "(untitled)", "location": scrub(location)}
