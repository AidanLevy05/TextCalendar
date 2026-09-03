from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from signal_calendar_bot.config import (  # noqa: E402
    ConfirmationConfig,
    ProtectedBlock,
    SchedulingConfig,
)
from signal_calendar_bot.store import Store  # noqa: E402

TZ_NAME = "America/New_York"


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


@pytest.fixture
def scheduling_cfg() -> SchedulingConfig:
    return SchedulingConfig(
        day_start="09:00",
        day_end="21:00",
        buffer_minutes=15,
        slot_granularity_minutes=15,
        max_candidates=3,
        protected_blocks=[
            ProtectedBlock(name="Jiu-jitsu", days=["mon", "wed", "fri"], start="18:30", end="20:00")
        ],
    )


@pytest.fixture
def confirm_cfg() -> ConfirmationConfig:
    return ConfirmationConfig(timeout_seconds=60)
