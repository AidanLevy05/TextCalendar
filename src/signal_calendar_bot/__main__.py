"""CLI entry point.

    signal-calendar-bot run       # the daemon
    signal-calendar-bot doctor    # check every dependency before you trust it
    signal-calendar-bot auth      # one-time Google OAuth
    signal-calendar-bot purge     # delete every event the bot ever created
    signal-calendar-bot send      # send a test message to Note to Self
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, datetime, timedelta

from .config import Config, find_config_file, load_config
from .logging_setup import setup_logging

log = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="signal-calendar-bot")
    parser.add_argument("-c", "--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="run the daemon")
    sub.add_parser("doctor", help="check config, Claude API, Signal, and Google")
    sub.add_parser("auth", help="run the one-time Google OAuth flow")

    purge = sub.add_parser("purge", help="delete every event tagged signal-bot")
    purge.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    purge.add_argument("--days", type=int, default=365, help="window each side, default 365")

    send = sub.add_parser("send", help="send a test message to Note to Self")
    send.add_argument("text", help="message body")

    return parser


def cmd_run(cfg: Config) -> int:
    from .daemon import run

    return run(cfg)


def cmd_auth(cfg: Config) -> int:
    from .gcal.auth import get_credentials

    creds = get_credentials(cfg.google, allow_interactive=True)
    print(f"OK — token written to {cfg.google.token_path}")
    print(f"     scopes: {', '.join(creds.scopes or [])}")
    return 0


def cmd_send(cfg: Config, text: str) -> int:
    from .signal_client import SignalClient

    client = SignalClient(cfg.signal)
    client.connect()
    try:
        client.send_note_to_self(text)
    finally:
        client.close()
    print("sent")
    return 0


def cmd_purge(cfg: Config, *, assume_yes: bool, days: int) -> int:
    """One-command removal of everything the bot created.

    This is why every created event carries
    extendedProperties.private.source = "signal-bot".
    """
    from .formatting import slot_label
    from .gcal.client import CalendarClient

    calendar = CalendarClient(cfg.google, cfg.general.tz)
    now = datetime.now(UTC)
    window = (now - timedelta(days=days), now + timedelta(days=days))
    events = calendar.find_bot_events(window)

    if not events:
        print("No bot-created events found.")
        return 0

    print(f"{len(events)} event(s) created by this bot:")
    for ev in events:
        print(f"  {slot_label(ev.start, ev.end, cfg.general.tz)}  {ev.title}")

    if not assume_yes:
        answer = input(f"\nDelete all {len(events)}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted. Nothing deleted.")
            return 1

    failed = 0
    for ev in events:
        try:
            calendar.delete_event(ev.id)
        except Exception as exc:  # noqa: BLE001 - report and keep going
            failed += 1
            print(f"  failed to delete {ev.id}: {exc}", file=sys.stderr)
    print(f"Deleted {len(events) - failed} event(s); {failed} failure(s).")
    return 1 if failed else 0


def cmd_doctor(cfg: Config, config_path) -> int:
    """Check every moving part. Run this before trusting the daemon."""
    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "PASS" if passed else "FAIL"
        print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
        ok = ok and passed

    print("=== config ===")
    check("config file found", config_path is not None, str(config_path or "using defaults"))
    check("timezone resolves", True, cfg.general.timezone)
    check("data dir writable", _writable(cfg.general.data_path), str(cfg.general.data_path))
    check(
        "account set",
        bool(cfg.signal.account) and cfg.signal.account.startswith("+"),
        cfg.signal.account or "(empty — set signal.account)",
    )
    check(
        "sender allowlist non-empty",
        bool(cfg.signal.allowed_senders),
        f"{len(cfg.signal.allowed_senders)} entry(ies)",
    )
    check(
        "confirmation window",
        True,
        f"{cfg.confirmation.timeout_seconds}s (writes expire after this)",
    )

    print("\n=== files ===")
    secret = cfg.google.client_secret_path
    check("google client secret present", secret.is_file(), str(secret))
    token = cfg.google.token_path
    check("google token present", token.is_file(), str(token))
    if token.is_file():
        mode = token.stat().st_mode & 0o777
        check("token permissions are 0600", mode == 0o600, oct(mode))

    print("\n=== claude api ===")
    from .llm import ClaudeClient, LLMAuthError

    key = cfg.anthropic.resolve_api_key()
    source = (
        "ANTHROPIC_API_KEY env var"
        if os.environ.get("ANTHROPIC_API_KEY", "").strip()
        else f"file {cfg.anthropic.api_key_path}"
    )
    check("api key found", key is not None, source if key else "not set — see docs/SETUP.md")
    if key is not None:
        check(
            "api key looks well-formed",
            key.startswith("sk-ant-"),
            "starts with sk-ant-" if key.startswith("sk-ant-") else "unexpected prefix",
        )
        if cfg.anthropic.api_key_path.is_file():
            mode = cfg.anthropic.api_key_path.stat().st_mode & 0o777
            check("key file permissions are 0600", mode == 0o600, oct(mode))
        try:
            llm = ClaudeClient(cfg.anthropic)
            ok_model, detail = llm.health()
            check(f"model {cfg.anthropic.model} reachable", ok_model, detail)
            llm.close()
        except LLMAuthError as exc:
            check("claude client", False, str(exc)[:200])
    check(
        "effort configured",
        True,
        f"{cfg.anthropic.effort} (parsing), {cfg.anthropic.phrase_effort} (phrasing)",
    )

    print("\n=== signal ===")
    from .signal_client import SignalClient

    client = SignalClient(cfg.signal)
    try:
        client.connect()
        check("signal-cli socket", True, cfg.signal.transport)
        try:
            version = client.call("version", {}, timeout=10)
            check("signal-cli responds", True, str(version))
        except Exception as exc:  # noqa: BLE001
            check("signal-cli responds", False, str(exc)[:200])
    except Exception as exc:  # noqa: BLE001
        check("signal-cli socket", False, str(exc)[:200])
    finally:
        client.close()

    print("\n=== google calendar ===")
    from .gcal.auth import ReauthRequired
    from .gcal.client import CalendarClient

    try:
        calendar = CalendarClient(cfg.google, cfg.general.tz)
        now = datetime.now(UTC)
        busy = calendar.freebusy((now, now + timedelta(days=1)))
        check("freebusy query", True, f"{len(busy)} busy block(s) in the next 24h")
    except ReauthRequired as exc:
        check("google credentials", False, str(exc)[:200])
    except Exception as exc:  # noqa: BLE001
        check("google calendar reachable", False, str(exc)[:200])

    print("\n" + ("All checks passed." if ok else "Some checks failed — see above."))
    return 0 if ok else 1


def _writable(path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except Exception as exc:  # noqa: BLE001 - config errors must be readable
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg.logging)

    if args.command == "run":
        return cmd_run(cfg)
    if args.command == "doctor":
        return cmd_doctor(cfg, find_config_file(args.config))
    if args.command == "auth":
        return cmd_auth(cfg)
    if args.command == "purge":
        return cmd_purge(cfg, assume_yes=args.yes, days=args.days)
    if args.command == "send":
        return cmd_send(cfg, args.text)
    return 2


if __name__ == "__main__":
    sys.exit(main())
