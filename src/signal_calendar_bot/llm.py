"""Ollama client. The model's entire job lives here.

Two calls, and only two:

* `parse_intent` — text in, validated `ParsedIntent` out. On schema failure it
  retries once with the validation error appended; on a second failure it
  returns UNKNOWN and the user is asked to rephrase. It never gets a third try
  and it never gets to emit anything that isn't in `models.py`.
* `phrase` — facts in, one sentence out. Purely cosmetic; every fact it is given
  was already computed by code, and a failure here degrades to the plain
  fallback text rather than blocking the operation.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from .config import OllamaConfig
from .models import ParsedIntent, UnknownIntent, validate_intent

log = logging.getLogger(__name__)

# Prompts ship with the package. Set SCB_PROMPT_DIR to point at edited copies
# without reinstalling.
PACKAGE_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def _prompt_dir() -> Path:
    override = os.environ.get("SCB_PROMPT_DIR")
    if override:
        return Path(override).expanduser()
    return PACKAGE_PROMPT_DIR

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
# Small models like to narrate before the JSON; take the outermost object.
_OBJECT = re.compile(r"\{.*\}", re.S)


class LLMUnavailable(RuntimeError):
    """Ollama could not be reached or did not answer in time."""


def _load_prompt(name: str) -> str:
    path = _prompt_dir() / name
    if not path.is_file():
        raise FileNotFoundError(f"prompt template missing: {path}")
    return path.read_text(encoding="utf-8")


def _render(template: str, **values: str) -> str:
    """Substitute {{name}} placeholders.

    Deliberately not str.format: the intent prompt is full of literal JSON
    braces in its examples, which format() would try to interpret.
    """
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def extract_json(raw: str) -> dict[str, Any]:
    """Pull a JSON object out of a model response that may be wrapped in prose."""
    candidate = raw.strip()
    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        match = _OBJECT.search(candidate)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("model returned JSON that is not an object")
    return parsed


class OllamaClient:
    def __init__(self, cfg: OllamaConfig) -> None:
        self.cfg = cfg
        self._client = httpx.Client(
            base_url=cfg.host.rstrip("/"), timeout=cfg.timeout_seconds
        )

    def close(self) -> None:
        self._client.close()

    def health(self) -> bool:
        try:
            self._client.get("/api/tags", timeout=5.0).raise_for_status()
            return True
        except httpx.HTTPError:
            return False

    def _generate(self, system: str, user: str, *, force_json: bool) -> str:
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "system": system,
            "prompt": user,
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
            "options": {
                "temperature": self.cfg.temperature,
                "num_ctx": self.cfg.num_ctx,
            },
        }
        if force_json:
            payload["format"] = "json"
        try:
            response = self._client.post("/api/generate", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"ollama request failed: {exc}") from exc
        return response.json().get("response", "")

    # -- the only two model calls in the project ---------------------------

    def parse_intent(self, message: str, *, now: datetime, tz: ZoneInfo) -> ParsedIntent:
        """Turn a message into a validated intent. Never raises on bad model output."""
        local_now = now.astimezone(tz)
        system = _render(
            _load_prompt("intent_system.md"),
            now_local=local_now.strftime("%Y-%m-%dT%H:%M:%S%z"),
            timezone=str(tz),
            today_name=local_now.strftime("%A"),
            today_date=local_now.strftime("%Y-%m-%d"),
        )

        attempt_prompt = message
        last_error = ""
        for attempt in (1, 2):
            raw = self._generate(system, attempt_prompt, force_json=True)
            log.debug("model intent output", extra={"attempt": attempt, "raw": raw[:1000]})
            try:
                intent = validate_intent(extract_json(raw))
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                last_error = str(exc)[:600]
                log.warning(
                    "intent validation failed",
                    extra={"attempt": attempt, "error": last_error},
                )
                # Retry once with the validation error appended, per the spec.
                attempt_prompt = (
                    f"{message}\n\n"
                    f"Your previous answer was rejected by the schema validator:\n"
                    f"{last_error}\n"
                    f"Return corrected JSON only."
                )
                continue
            log.info(
                "intent parsed",
                extra={"intent": intent.intent.value, "attempt": attempt},
            )
            return intent

        log.error("intent unparseable after retry", extra={"error": last_error})
        return UnknownIntent(reason="could not understand that")

    def phrase(self, facts: str, *, fallback: str) -> str:
        """Phrase pre-computed facts. Degrades to `fallback` on any failure."""
        try:
            system = _render(_load_prompt("phrase_system.md"), facts=facts)
            text = self._generate(system, "Write the reply.", force_json=False).strip()
        except (LLMUnavailable, FileNotFoundError, OSError) as exc:
            log.warning("phrasing failed, using fallback", extra={"error": str(exc)})
            return fallback
        text = text.strip().strip('"')
        if not text or len(text) > 600:
            log.warning("phrasing rejected, using fallback", extra={"length": len(text)})
            return fallback
        return text
