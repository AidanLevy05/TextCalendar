#!/usr/bin/env bash
# Run the bot in the foreground from a shell.
#
# The systemd unit gets the API key via EnvironmentFile=. Running by hand does
# not go through systemd, so this script loads the same file itself — otherwise
# the key you put in secrets.env would be invisible here and the bot would exit
# with LLMAuthError.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/signal-calendar-bot"
SECRETS_FILE="${SCB_SECRETS_FILE:-$CONFIG_DIR/secrets.env}"

# An already-exported key wins; otherwise pull it from secrets.env if present.
# Absent is fine — the bot also accepts a bare-key file (anthropic.api_key_file).
if [[ -z "${ANTHROPIC_API_KEY:-}" && -r "$SECRETS_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
  set +a
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Missing .venv. Run: python3 -m venv .venv && source .venv/bin/activate && pip install -e ." >&2
  exit 1
fi

source ".venv/bin/activate"
exec python -m signal_calendar_bot run
