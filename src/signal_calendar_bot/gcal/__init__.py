"""Google Calendar access: OAuth and a narrow, tagging API client."""

from .auth import ReauthRequired, get_credentials, run_oauth_flow
from .client import BOT_SOURCE_TAG, CalendarClient

__all__ = [
    "BOT_SOURCE_TAG",
    "CalendarClient",
    "ReauthRequired",
    "get_credentials",
    "run_oauth_flow",
]
