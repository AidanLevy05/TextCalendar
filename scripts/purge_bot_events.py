#!/usr/bin/env python3
"""Delete every event this bot ever created.

Backed by `extendedProperties.private.source = "signal-bot"`, which is applied
to every created event. Nothing else on the calendar is touched.

    python scripts/purge_bot_events.py          # lists, then asks
    python scripts/purge_bot_events.py --yes    # no prompt
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from signal_calendar_bot.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["purge", *sys.argv[1:]]))
