# TextCalendar — Signal Calendar Agent

Manage your Google Calendar by texting Signal. Send "lunch with Dan Thursday" to
your own Note to Self; the bot finds an open slot, proposes it, and creates the
event **only** if you confirm within 60 seconds.

All LLM inference is local (Ollama). No calendar data leaves the machine.

```
Signal (phone) → Note to Self → signal-cli daemon (JSON-RPC)
                                        ↓
                              Python daemon (this repo)
                                   ↙         ↘
                          Ollama (local)   Google Calendar API
```

## Core design rule

**The model does language. Code does logic.**

The LLM has exactly two jobs: turn a text message into a validated JSON intent,
and phrase pre-computed results in natural language. It never does date
arithmetic, never decides whether two events conflict, and never picks a time
slot. All of that is deterministic Python in `scheduling.py`, unit-tested
without Ollama or Google running.

## The confirmation window

Reads (`query`) execute immediately. **Writes never do.**

The bot previews exactly what it will do and starts a 60-second fuse:

```
You:  lunch with dan thursday
Bot:  Lunch with Dan — Thursday 12pm-1pm. Also open: Friday 12pm-1pm.
      Reply YES within 60s to book it.
You:  yes
Bot:  Booked: Lunch with Dan Thursday 12pm-1pm.
```

Miss the window and the proposal is **destroyed** — nothing is written, and you
send the request again:

```
Bot:  'Lunch with Dan — Thursday 12pm-1pm' expired after 60 seconds.
      Nothing was changed.
```

This is deliberate and there is no grace period. A hallucinated delete on a real
calendar is the failure mode that kills this project, and the second-worst
version is a "yes" typed twenty minutes later landing on a proposal you stopped
thinking about. Each proposal gets exactly one reply: confirm, cancel, or lapse.
An unrecognized reply (`"actually make it friday"`) drops the proposal and is
treated as a fresh request — the bot never guesses at consent.

Tune the window with `confirmation.timeout_seconds`.

## Quick start

```bash
git clone <this repo> ~/TextCalendar && cd ~/TextCalendar
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

cp config.example.toml config.toml && chmod 600 config.toml
$EDITOR config.toml          # set signal.account to your number

# See docs/SETUP.md for keys, signal-cli linking, and Ollama.
python -m signal_calendar_bot doctor
python -m signal_calendar_bot run
```

**`doctor` is the command to trust.** It checks config, file permissions,
Ollama reachability, the configured model, the signal-cli socket, and a live
Google freebusy query, and tells you exactly what is missing.

## Setup

**[docs/SETUP.md](docs/SETUP.md)** — every credential, where it comes from, and
the exact file it goes in. Start there.

## Commands

| Command | What it does |
|---|---|
| `run` | Run the daemon |
| `doctor` | Check every dependency and permission |
| `auth` | One-time Google OAuth flow |
| `purge` | Delete every event the bot ever created |
| `send "text"` | Send a test message to Note to Self |

## Nothing is hardcoded to one machine

Model name, `num_ctx`, Ollama host, data paths, timezone, scheduling rules, and
the allowlist all come from `config.toml`. Moving from the Legion (RTX 4060,
8GB) to the MSI box (RTX 2060, 6GB) means changing one value:

```toml
[ollama]
model = "qwen3:4b"    # was qwen3:8b
```

Every setting also accepts an environment override, `SCB_<SECTION>_<KEY>`:

```bash
SCB_OLLAMA_MODEL=qwen3:4b SCB_CONFIRMATION_TIMEOUT_SECONDS=90 python -m signal_calendar_bot run
```

## Safety properties

These are invariants, not conventions, and each has tests:

- **Every created event is tagged** `extendedProperties.private.source = "signal-bot"`,
  applied inside `create_event` itself so no code path can write an untagged
  event. `purge` uses it to remove everything the bot ever made.
- **Writes require confirmation** inside the 60-second window. A lapsed proposal
  cannot be confirmed.
- **Messages are deduped** by `(source, timestamp)` in sqlite. signal-cli
  redelivers on reconnect; without this, one "add dentist 3pm" becomes two
  events.
- **Only allowlisted senders** are processed. Everything else is dropped and
  logged.
- **Calendar text is untrusted.** Titles from invites you did not write are
  scrubbed of prompt-control phrasing, zero-width characters, and delimiters,
  then fenced in `<untrusted>` tags before reaching any prompt.
- **Availability uses freebusy**, not `events.list` — smaller payload, and no
  event details from other people's meetings enter the process.
- **Everything internal is UTC**; conversion happens at the edges only.

## Reliability

Three systemd user units with `Restart=always` (see `systemd/`).

The failure mode that matters: signal-cli's websocket can die silently while the
process stays alive, so the unit looks healthy while receiving nothing forever.
The daemon proves the loop end-to-end instead — it sends itself a nonce via Note
to Self and expects that exact nonce back through the receive path. Two
consecutive misses restart the signal-cli unit.

## Logging

Structured JSON lines with a correlation id per message. For any inbound text
you can grep one id and see the raw text, the parsed intent, the freebusy
result, and the API call made:

```bash
grep '"cid":"a1b2c3d4e5f6"' ~/.local/share/signal-calendar-bot/bot.log | jq .
```

## Development

```bash
pip install -e '.[dev]'
pytest              # 57 tests, no network or model needed
ruff check src tests
```

The scheduling engine, confirmation state machine, dedupe, and sanitizer are all
tested without Ollama, signal-cli, or Google.

## Scope

**v1:** `create`, `query`, `move`, `delete`, `unknown`; primary calendar only;
Note to Self only.

**Not in v1:** recurring events, multiple calendars, attendee invitations, group
chats, proactive notifications.

**Designed for, not yet built:** morning agenda push, conflict detection on
incoming invites, gap detection.
