"""The Claude API layer. No network — schema shape and narrowing only."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from signal_calendar_bot.config import AnthropicConfig
from signal_calendar_bot.llm import ClaudeClient, LLMAuthError, RawIntent, _intent_schema
from signal_calendar_bot.models import CreateIntent, QueryIntent, UnknownIntent, validate_intent


def test_schema_is_flat_and_closed():
    """Structured outputs need a self-contained, closed schema."""
    schema = _intent_schema()
    assert schema["additionalProperties"] is False
    # $defs/$ref would require the API to resolve references; keep it flat.
    assert "$defs" not in schema
    assert schema["required"] == ["intent"]


def test_schema_covers_every_field_the_union_can_use():
    props = set(_intent_schema()["properties"])
    for field in (
        "intent", "title", "duration_minutes", "location", "attendees",
        "exact_time", "window_start", "window_end", "subject",
        "target_query", "target_window_start", "target_window_end", "reason",
    ):
        assert field in props


def test_unset_fields_are_dropped_before_strict_validation():
    """The strict union forbids extras, so None-valued keys must not reach it."""
    raw = RawIntent(intent="unknown", reason="not a calendar request")
    assert raw.to_intent_dict() == {"intent": "unknown", "reason": "not a calendar request"}


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            RawIntent(
                intent="create", title="Lunch with Dan", exact_time=False,
                window_start="2026-09-10T00:00:00-04:00",
                window_end="2026-09-10T23:59:00-04:00",
            ),
            CreateIntent,
        ),
        (
            RawIntent(
                intent="query", subject="Thursday",
                window_start="2026-09-10T00:00:00-04:00",
                window_end="2026-09-10T23:59:00-04:00",
            ),
            QueryIntent,
        ),
        (RawIntent(intent="unknown", reason="not a calendar request"), UnknownIntent),
    ],
)
def test_raw_payload_narrows_to_the_right_intent(raw, expected):
    assert isinstance(validate_intent(raw.to_intent_dict()), expected)


def test_business_rules_still_reject_a_schema_valid_payload():
    """Structured outputs guarantee shape, not sense. The union guards meaning."""
    backwards = RawIntent(
        intent="create", title="Lunch",
        window_start="2026-09-10T12:00:00-04:00",
        window_end="2026-09-09T12:00:00-04:00",  # ends before it starts
    )
    with pytest.raises(ValidationError):
        validate_intent(backwards.to_intent_dict())

    titleless = RawIntent(
        intent="create",
        window_start="2026-09-10T00:00:00-04:00",
        window_end="2026-09-10T23:59:00-04:00",
    )
    with pytest.raises(ValidationError):
        validate_intent(titleless.to_intent_dict())


def test_missing_api_key_fails_loudly_at_construction(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = AnthropicConfig(api_key_file=str(tmp_path / "nope"))
    with pytest.raises(LLMAuthError, match="No Anthropic API key"):
        ClaudeClient(cfg)


def test_api_key_comes_from_env_first(monkeypatch, tmp_path):
    key_file = tmp_path / "key"
    key_file.write_text("sk-ant-from-file")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    cfg = AnthropicConfig(api_key_file=str(key_file))
    assert cfg.resolve_api_key() == "sk-ant-from-env"


def test_api_key_falls_back_to_the_file(monkeypatch, tmp_path):
    key_file = tmp_path / "key"
    key_file.write_text("  sk-ant-from-file\n")  # trailing newline is normal
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = AnthropicConfig(api_key_file=str(key_file))
    assert cfg.resolve_api_key() == "sk-ant-from-file"


def test_blank_env_key_does_not_shadow_the_file(monkeypatch, tmp_path):
    key_file = tmp_path / "key"
    key_file.write_text("sk-ant-from-file")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    cfg = AnthropicConfig(api_key_file=str(key_file))
    assert cfg.resolve_api_key() == "sk-ant-from-file"


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_valid_effort_levels_accepted(effort):
    assert AnthropicConfig(effort=effort).effort == effort


def test_invalid_effort_is_rejected():
    with pytest.raises(ValueError, match="effort must be one of"):
        AnthropicConfig(effort="turbo")


def test_config_has_no_sampling_knobs():
    """Sonnet 5 returns 400 for temperature/top_p/top_k — they must not exist."""
    fields = set(AnthropicConfig.model_fields)
    assert not fields & {"temperature", "top_p", "top_k", "num_ctx", "keep_alive"}
