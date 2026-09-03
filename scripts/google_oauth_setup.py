#!/usr/bin/env python3
"""One-time Google OAuth setup.

Run this on the machine that will host the bot, with a browser available:

    python scripts/google_oauth_setup.py

It reads `google.client_secret_file` from your config, opens a browser for
consent, and writes `google.token_file` with mode 0600.

If the host is headless, run it once on a desktop with the same config and copy
the resulting token.json across (still 0600) — the refresh token is not tied to
the machine.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from signal_calendar_bot.config import load_config  # noqa: E402
from signal_calendar_bot.gcal.auth import get_credentials  # noqa: E402


def main() -> int:
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)

    secret = cfg.google.client_secret_path
    if not secret.is_file():
        print(f"ERROR: OAuth client secret not found at:\n  {secret}\n", file=sys.stderr)
        print(
            "Get it from https://console.cloud.google.com/apis/credentials\n"
            "  APIs & Services -> Credentials -> Create credentials\n"
            "  -> OAuth client ID -> Application type: Desktop app\n"
            "Then download the JSON and save it to the path above.",
            file=sys.stderr,
        )
        return 1

    print(f"Client secret: {secret}")
    print(f"Token will be written to: {cfg.google.token_path}")
    print("A browser window will open for consent.\n")

    creds = get_credentials(cfg.google, allow_interactive=True)

    print("\nSuccess.")
    print(f"  token file : {cfg.google.token_path}")
    print(f"  scopes     : {', '.join(creds.scopes or [])}")
    print(f"  refresh    : {'yes' if creds.refresh_token else 'NO — re-run with prompt=consent'}")
    print(
        "\nReminder: publish the OAuth consent screen to Production. An external\n"
        "app left in Testing has refresh tokens that expire after 7 days, which\n"
        "will look exactly like a bug in the bot."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
