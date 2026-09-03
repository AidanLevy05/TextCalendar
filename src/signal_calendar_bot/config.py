"""Configuration loading.

Single source of truth for every tunable. Nothing in this project may read a
machine-specific path, model name, or number from anywhere else, so the whole
thing moves between the Legion and the MSI box by editing one file.

Precedence (highest wins):
    1. Environment variable  SCB_<SECTION>_<KEY>
    2. config.toml
    3. Defaults in this module
"""

from __future__ import annotations

import os
import tomllib
from datetime import time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ENV_PREFIX = "SCB"

# Search order for config.toml when --config is not passed.
DEFAULT_CONFIG_PATHS = (
    Path("./config.toml"),
    Path("~/.config/signal-calendar-bot/config.toml"),
    Path("/etc/signal-calendar-bot/config.toml"),
)


def expand(p: str | Path) -> Path:
    """Expand ~ and $VARS, return an absolute path."""
    return Path(os.path.expandvars(str(p))).expanduser().resolve()


def _parse_hhmm(value: str) -> dt_time:
    hh, _, mm = value.partition(":")
    return dt_time(hour=int(hh), minute=int(mm or 0))


class GeneralConfig(BaseModel):
    timezone: str = "America/New_York"
    data_dir: str = "~/.local/share/signal-calendar-bot"

    @field_validator("timezone")
    @classmethod
    def _tz_must_resolve(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:  # pragma: no cover - env dependent
            raise ValueError(
                f"unknown timezone {v!r}; install the 'tzdata' package or fix the name"
            ) from exc
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def data_path(self) -> Path:
        return expand(self.data_dir)


class LoggingConfig(BaseModel):
    # `json` in the TOML file; renamed here because BaseModel already has .json
    model_config = ConfigDict(populate_by_name=True)

    level: str = "INFO"
    file: str = "~/.local/share/signal-calendar-bot/bot.log"
    json_lines: bool = Field(default=True, alias="json")
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5

    @property
    def path(self) -> Path:
        return expand(self.file)


class SignalConfig(BaseModel):
    transport: str = "unix"
    socket_path: str = "~/.local/share/signal-cli/socket"
    host: str = "127.0.0.1"
    port: int = 7583
    account: str = ""
    allowed_senders: list[str] = Field(default_factory=list)
    note_to_self_only: bool = True

    @field_validator("transport")
    @classmethod
    def _known_transport(cls, v: str) -> str:
        if v not in ("unix", "tcp"):
            raise ValueError("signal.transport must be 'unix' or 'tcp'")
        return v

    @model_validator(mode="after")
    def _account_in_allowlist(self) -> SignalConfig:
        if self.account and self.account not in self.allowed_senders:
            self.allowed_senders = [self.account, *self.allowed_senders]
        return self

    @property
    def socket(self) -> Path:
        return expand(self.socket_path)


class OllamaConfig(BaseModel):
    host: str = "http://127.0.0.1:11434"
    model: str = "qwen3:8b"
    num_ctx: int = 8192
    temperature: float = 0.0
    timeout_seconds: float = 90.0
    keep_alive: str = "-1"


class GoogleConfig(BaseModel):
    client_secret_file: str = "~/.config/signal-calendar-bot/client_secret.json"
    token_file: str = "~/.config/signal-calendar-bot/token.json"
    calendar_id: str = "primary"
    oauth_local_port: int = 8765

    @property
    def client_secret_path(self) -> Path:
        return expand(self.client_secret_file)

    @property
    def token_path(self) -> Path:
        return expand(self.token_file)


class ConfirmationConfig(BaseModel):
    """The write-confirmation window.

    A proposal lives for exactly `timeout_seconds`. Miss it and the proposal is
    destroyed — no write happens and the request must be sent again. This is
    deliberate: a "yes" that arrives late must never be able to attach itself to
    a proposal the user has stopped thinking about.
    """

    timeout_seconds: int = 60
    notify_on_expiry: bool = True
    affirmatives: list[str] = Field(
        default_factory=lambda: ["yes", "y", "yeah", "yep", "ok", "okay", "confirm", "do it", "go"]
    )
    negatives: list[str] = Field(
        default_factory=lambda: ["no", "n", "nope", "cancel", "stop", "nevermind", "abort"]
    )

    @field_validator("timeout_seconds")
    @classmethod
    def _sane_window(cls, v: int) -> int:
        if v < 5:
            raise ValueError("confirmation.timeout_seconds below 5s is unusable")
        if v > 3600:
            raise ValueError("confirmation.timeout_seconds above 1h defeats the purpose")
        return v


class ProtectedBlock(BaseModel):
    name: str
    days: list[str]
    start: str
    end: str

    @property
    def start_time(self) -> dt_time:
        return _parse_hhmm(self.start)

    @property
    def end_time(self) -> dt_time:
        return _parse_hhmm(self.end)

    @property
    def weekdays(self) -> set[int]:
        """Python weekday numbers (Mon=0) this block applies to."""
        names = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        out: set[int] = set()
        for d in self.days:
            key = d.strip().lower()[:3]
            if key not in names:
                raise ValueError(f"unknown day {d!r} in protected block {self.name!r}")
            out.add(names[key])
        return out


class SchedulingConfig(BaseModel):
    day_start: str = "09:00"
    day_end: str = "21:00"
    buffer_minutes: int = 15
    slot_granularity_minutes: int = 15
    max_candidates: int = 3
    max_search_days: int = 14
    default_durations: dict[str, int] = Field(
        default_factory=lambda: {
            "lunch": 60,
            "call": 30,
            "meeting": 60,
            "gym": 90,
            "_default": 60,
        }
    )
    protected_blocks: list[ProtectedBlock] = Field(default_factory=list)

    @property
    def day_start_time(self) -> dt_time:
        return _parse_hhmm(self.day_start)

    @property
    def day_end_time(self) -> dt_time:
        return _parse_hhmm(self.day_end)

    def duration_for(self, title: str) -> int:
        """Pick a default duration from keywords in the title.

        Longest keyword wins so "lunch meeting" resolves to lunch, not meeting.
        """
        lowered = title.lower()
        best: tuple[int, int] | None = None  # (keyword length, minutes)
        for keyword, minutes in self.default_durations.items():
            if keyword.startswith("_"):
                continue
            if keyword.lower() in lowered:
                if best is None or len(keyword) > best[0]:
                    best = (len(keyword), minutes)
        if best is not None:
            return best[1]
        return int(self.default_durations.get("_default", 60))


class HeartbeatConfig(BaseModel):
    enabled: bool = True
    interval_seconds: int = 900
    timeout_seconds: int = 60
    restart_command: str = "systemctl --user restart signal-cli.service"
    max_missed: int = 2


class Config(BaseModel):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    signal: SignalConfig = Field(default_factory=SignalConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    google: GoogleConfig = Field(default_factory=GoogleConfig)
    confirmation: ConfirmationConfig = Field(default_factory=ConfirmationConfig)
    scheduling: SchedulingConfig = Field(default_factory=SchedulingConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)

    @property
    def db_path(self) -> Path:
        return self.general.data_path / "state.db"

    def ensure_dirs(self) -> None:
        """Create the data/log/token directories with owner-only permissions."""
        for d in (
            self.general.data_path,
            self.logging.path.parent,
            self.google.token_path.parent,
        ):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)


def _coerce(raw: str) -> Any:
    """Turn an env-var string into the closest TOML-ish scalar."""
    low = raw.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    if raw.strip().startswith("[") or "," in raw:
        return [part.strip() for part in raw.strip("[]").split(",") if part.strip()]
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Overlay SCB_<SECTION>_<KEY> environment variables onto parsed TOML."""
    sections = set(Config.model_fields)
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(ENV_PREFIX + "_"):
            continue
        remainder = env_key[len(ENV_PREFIX) + 1 :].lower()
        section, _, key = remainder.partition("_")
        if not key or section not in sections:
            continue
        data.setdefault(section, {})[key] = _coerce(env_val)
    return data


def find_config_file(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    if explicit:
        p = expand(explicit)
        if not p.is_file():
            raise FileNotFoundError(f"config file not found: {p}")
        return p
    env_path = os.environ.get("SCB_CONFIG")
    if env_path:
        p = expand(env_path)
        if not p.is_file():
            raise FileNotFoundError(f"SCB_CONFIG points at a missing file: {p}")
        return p
    for candidate in DEFAULT_CONFIG_PATHS:
        p = expand(candidate)
        if p.is_file():
            return p
    return None


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load config from TOML plus environment overrides."""
    cfg_file = find_config_file(path)
    data: dict[str, Any] = {}
    if cfg_file is not None:
        with cfg_file.open("rb") as fh:
            data = tomllib.load(fh)
    data = _apply_env_overrides(data)
    cfg = Config.model_validate(data)
    cfg.ensure_dirs()
    return cfg
