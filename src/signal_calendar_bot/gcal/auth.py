"""Google OAuth: desktop flow, local refresh token, tight file permissions.

Two failure modes are handled explicitly because both otherwise look like a bug
in the bot rather than a Google-side condition:

* **`invalid_grant`.** The refresh token was revoked, expired, or the consent
  screen is still in Testing (where refresh tokens die after 7 days). We raise
  `ReauthRequired` so the daemon can tell the user to re-run the setup script,
  instead of crashing in a restart loop.
* **Missing client secret.** Raised with the exact path we looked in.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from ..config import GoogleConfig

log = logging.getLogger(__name__)

# Least privilege, and exactly the two the project needs:
#   calendar.events   — read + write events (list, insert, patch, delete)
#   calendar.freebusy — availability only, no event details
# Deliberately NOT calendar.readonly (reads every calendar) or calendar (full
# access including settings and ACLs).
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
]


class ReauthRequired(RuntimeError):
    """The stored refresh token is unusable; a human must re-run the OAuth flow."""


def _write_token(path: Path, creds: Credentials) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Create with 0600 from the start — never briefly world-readable.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    os.replace(tmp, path)
    path.chmod(0o600)
    log.info("stored google credentials", extra={"token_file": str(path)})


def _load_token(path: Path) -> Credentials | None:
    if not path.is_file():
        return None
    try:
        return Credentials.from_authorized_user_file(str(path), SCOPES)
    except (ValueError, json.JSONDecodeError) as exc:
        log.warning("token file unreadable, ignoring", extra={"error": str(exc)})
        return None


def run_oauth_flow(cfg: GoogleConfig) -> Credentials:
    """Interactive, one-time. Opens a browser and writes the token file."""
    secret = cfg.client_secret_path
    if not secret.is_file():
        raise FileNotFoundError(
            f"Google OAuth client secret not found at {secret}.\n"
            "Download it from Google Cloud Console -> APIs & Services -> Credentials\n"
            "(OAuth client ID, type 'Desktop app') and save it to that exact path."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(
        port=cfg.oauth_local_port,
        access_type="offline",
        prompt="consent",  # force a refresh token even on re-auth
        open_browser=True,
    )
    _write_token(cfg.token_path, creds)
    return creds


def get_credentials(cfg: GoogleConfig, *, allow_interactive: bool = False) -> Credentials:
    """Load cached credentials, refreshing silently when possible.

    The daemon calls this with allow_interactive=False: it must never block on a
    browser prompt. The setup script calls it with True.
    """
    creds = _load_token(cfg.token_path)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _write_token(cfg.token_path, creds)
            return creds
        except RefreshError as exc:
            log.error("refresh token rejected", extra={"error": str(exc)})
            if not allow_interactive:
                raise ReauthRequired(
                    "Google refused the refresh token (invalid_grant). Usually this means "
                    "the OAuth consent screen is still in Testing (tokens expire after 7 "
                    "days) or access was revoked. Re-run: python scripts/google_oauth_setup.py"
                ) from exc

    if not allow_interactive:
        raise ReauthRequired(
            f"No usable Google credentials at {cfg.token_path}. "
            "Run: python scripts/google_oauth_setup.py"
        )
    return run_oauth_flow(cfg)
