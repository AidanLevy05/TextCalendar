"""Claude API client. The model's entire job lives here.

Two calls, and only two:

* `parse_intent` — text in, validated `ParsedIntent` out. Structured outputs
  guarantee the JSON is well-formed and matches the field schema; the strict
  pydantic union in `models.py` then enforces the business rules on top (a
  `create` needs a title, windows must be sane, and so on). On a rule failure it
  retries once with the validation error appended, then gives up and returns
  UNKNOWN so the user is asked to rephrase.
* `phrase` — facts in, one sentence out. Purely cosmetic: every fact it is given
  was already computed by code, so a failure here degrades to the plain fallback
  text rather than blocking the operation. Runs at lower effort accordingly.

Model-specific constraints that are not optional:

* **No `temperature` / `top_p` / `top_k`.** Sonnet 5 rejects all three with a
  400. Determinism comes from structured outputs and the strict schema, not from
  a sampling knob.
* **No `budget_tokens`.** Removed on this model class; depth is controlled by
  `output_config.effort` instead.
* **Thinking is left at its default (adaptive).** Explicitly disabling it has
  known failure modes and buys nothing here.

Prompt layout is chosen for cache stability: the system prompt is byte-identical
on every request, and everything volatile (the current time, the user's message)
goes in the user turn. Putting the clock in the system prompt would invalidate
the cached prefix on literally every call.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import anthropic
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import AnthropicConfig
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


class LLMUnavailable(RuntimeError):
    """The API could not be reached, timed out, or rate-limited us."""


class LLMAuthError(LLMUnavailable):
    """The API key is missing, malformed, or rejected. Needs human action."""


def _load_prompt(name: str) -> str:
    path = _prompt_dir() / name
    if not path.is_file():
        raise FileNotFoundError(f"prompt template missing: {path}")
    return path.read_text(encoding="utf-8")


class RawIntent(BaseModel):
    """The exact shape the model is constrained to emit.

    Flat and fully optional by design. Structured outputs guarantee we get these
    field names with these types; `validate_intent` then narrows this into the
    strict per-intent union, which is where the real rules live. Splitting it
    this way means a schema the model can always satisfy, without weakening the
    validation that protects the calendar.
    """

    model_config = ConfigDict(extra="forbid")

    intent: Literal["create", "query", "move", "delete", "unknown"]
    title: str | None = Field(default=None, description="Short event name")
    duration_minutes: int | None = Field(
        default=None, description="Only if the user stated a length"
    )
    location: str | None = Field(default=None, description="Only if a place was named")
    attendees: list[str] | None = Field(default=None, description="Names mentioned")
    exact_time: bool | None = Field(
        default=None, description="True if the user named a specific start time"
    )
    window_start: str | None = Field(default=None, description="ISO-8601 with offset")
    window_end: str | None = Field(default=None, description="ISO-8601 with offset")
    subject: str | None = Field(default=None, description="For query: what was asked")
    target_query: str | None = Field(
        default=None, description="For move/delete: words identifying the event"
    )
    target_window_start: str | None = Field(default=None, description="ISO-8601 with offset")
    target_window_end: str | None = Field(default=None, description="ISO-8601 with offset")
    reason: str | None = Field(default=None, description="For unknown: what was unclear")

    def to_intent_dict(self) -> dict[str, Any]:
        """Drop unset fields so the strict union sees only what was supplied."""
        return self.model_dump(exclude_none=True)


def _intent_schema() -> dict[str, Any]:
    schema = RawIntent.model_json_schema()
    schema["additionalProperties"] = False
    return schema


class ClaudeClient:
    def __init__(self, cfg: AnthropicConfig) -> None:
        self.cfg = cfg
        api_key = cfg.resolve_api_key()
        if not api_key:
            raise LLMAuthError(
                "No Anthropic API key found. Set ANTHROPIC_API_KEY in the "
                f"environment, or put the key in {cfg.api_key_path} (mode 0600)."
            )
        self._client = anthropic.Anthropic(
            api_key=api_key,
            timeout=cfg.timeout_seconds,
            max_retries=cfg.max_retries,
        )

    def close(self) -> None:
        # The SDK manages its own connection pool; nothing to release explicitly.
        pass

    def health(self) -> tuple[bool, str]:
        """Check that the key works and the configured model is reachable.

        Uses the Models API, which is cheap and does not run inference.
        """
        try:
            model = self._client.models.retrieve(self.cfg.model)
        except anthropic.AuthenticationError:
            return False, "API key rejected (401). Check ANTHROPIC_API_KEY."
        except anthropic.PermissionDeniedError:
            return False, "API key lacks permission for this model (403)."
        except anthropic.NotFoundError:
            return False, f"Model {self.cfg.model!r} not found for this account."
        except anthropic.APIConnectionError as exc:
            return False, f"Could not reach the API: {exc}"
        except anthropic.APIStatusError as exc:
            return False, f"API error {exc.status_code}: {exc}"
        return True, getattr(model, "display_name", self.cfg.model)

    # -- plumbing ----------------------------------------------------------

    def _call(
        self,
        *,
        system: str,
        user: str,
        effort: str,
        max_tokens: int,
        output_format: dict[str, Any] | None = None,
    ) -> str:
        output_config: dict[str, Any] = {"effort": effort}
        if output_format is not None:
            output_config["format"] = output_format

        try:
            response = self._client.messages.create(
                model=self.cfg.model,
                max_tokens=max_tokens,
                # Stable prefix, marked cacheable. Only pays off once the prompt
                # exceeds the model's minimum cacheable length; harmless below it.
                system=[
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user}],
                output_config=output_config,
            )
        except anthropic.AuthenticationError as exc:
            raise LLMAuthError(f"Anthropic rejected the API key: {exc}") from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMAuthError(f"API key lacks permission: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMUnavailable(f"rate limited: {exc}") from exc
        except anthropic.APITimeoutError as exc:
            raise LLMUnavailable(f"request timed out: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable(f"could not reach the API: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMUnavailable(f"API error {exc.status_code}: {exc}") from exc

        if response.stop_reason == "refusal":
            detail = getattr(response.stop_details, "category", None)
            log.warning("model refused the request", extra={"category": detail})
            raise LLMUnavailable(f"model declined the request (category={detail})")

        log.debug(
            "claude call",
            extra={
                "effort": effort,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read": getattr(response.usage, "cache_read_input_tokens", 0),
                "stop_reason": response.stop_reason,
            },
        )
        return "".join(b.text for b in response.content if b.type == "text")

    # -- the only two model calls in the project ---------------------------

    def parse_intent(self, message: str, *, now: datetime, tz: ZoneInfo) -> ParsedIntent:
        """Turn a message into a validated intent. Never raises on bad model output."""
        local_now = now.astimezone(tz)
        system = _load_prompt("intent_system.md")
        context = (
            f"CURRENT TIME: {local_now.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
            f"TIMEZONE: {tz}\n"
            f"TODAY IS: {local_now.strftime('%A')}, {local_now.strftime('%Y-%m-%d')}\n"
        )
        schema = {"type": "json_schema", "schema": _intent_schema()}

        user = f"{context}\nMESSAGE:\n{message}"
        last_error = ""
        for attempt in (1, 2):
            raw = self._call(
                system=system,
                user=user,
                effort=self.cfg.effort,
                max_tokens=self.cfg.max_tokens,
                output_format=schema,
            )
            try:
                payload = RawIntent.model_validate_json(raw)
                intent = validate_intent(payload.to_intent_dict())
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                last_error = str(exc)[:600]
                log.warning(
                    "intent validation failed",
                    extra={"attempt": attempt, "error": last_error},
                )
                # Retry once with the validation error appended.
                user = (
                    f"{context}\nMESSAGE:\n{message}\n\n"
                    f"Your previous answer was rejected by the validator:\n{last_error}\n"
                    "Return corrected JSON."
                )
                continue
            log.info("intent parsed", extra={"intent": intent.intent.value, "attempt": attempt})
            return intent

        log.error("intent unparseable after retry", extra={"error": last_error})
        return UnknownIntent(reason="could not understand that")

    def phrase(self, facts: str, *, fallback: str) -> str:
        """Phrase pre-computed facts. Degrades to `fallback` on any failure."""
        try:
            text = self._call(
                system=_load_prompt("phrase_system.md"),
                user=f"FACTS:\n{facts}\n\nWrite the reply.",
                effort=self.cfg.phrase_effort,
                max_tokens=self.cfg.phrase_max_tokens,
            ).strip()
        except (LLMUnavailable, FileNotFoundError, OSError) as exc:
            log.warning("phrasing failed, using fallback", extra={"error": str(exc)})
            return fallback
        text = text.strip().strip('"')
        if not text or len(text) > 600:
            log.warning("phrasing rejected, using fallback", extra={"length": len(text)})
            return fallback
        return text
