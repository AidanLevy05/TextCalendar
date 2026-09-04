# TextCalendar — Signal Calendar Agent

Manage your Google Calendar by texting Signal. Send "lunch with Dan Thursday" to
your own Note to Self; the bot finds an open slot, proposes it, and creates the
event **only** if you confirm within 60 seconds.

Language is handled by **Claude Sonnet 5** at medium effort. Scheduling is not —
see the design rule below.

```
Signal (phone) → Note to Self → signal-cli daemon (JSON-RPC)
                                        ↓
                              Python daemon (this repo)
                                   ↙         ↘
                     Claude API (Sonnet 5)   Google Calendar API
```

> **Privacy.** Event titles and times are sent to Anthropic's API to be parsed
> and phrased. Availability lookups use Google's freebusy endpoint, so other
> people's event details never enter the process — but your own titles do leave
> the machine.

## Core design rule

**The model does language. Code does logic.**

The LLM has exactly two jobs: turn a text message into a validated JSON intent,
and phrase pre-computed results in natural language. It never does date
arithmetic, never decides whether two events conflict, and never picks a time
slot. All of that is deterministic Python in `scheduling.py`, unit-tested
without the API or Google running.

Structured outputs constrain the model to a fixed schema, and the strict pydantic
union in `models.py` then enforces the rules on top — a `create` needs a title, a
window has to end after it starts. Shape is guaranteed by the API; meaning is
guaranteed by our own code.

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

# See docs/SETUP.md for the API key, signal-cli linking, and Google OAuth.
python -m signal_calendar_bot doctor
python -m signal_calendar_bot run
```

**`doctor` is the command to trust.** It checks config, file permissions, the
Anthropic key (and where it came from), the configured model, the signal-cli
socket, and a live Google freebusy query, and tells you exactly what is missing.

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

Model, effort, data paths, timezone, scheduling rules, and the allowlist all come
from `config.toml`. Since inference is now an API call rather than local GPU work,
moving from the Legion to the headless MSI box needs no model change at all — the
box only has to reach the network.

```toml
[anthropic]
model  = "claude-sonnet-5"
effort = "medium"        # low | medium | high | xhigh | max
```

Every setting also accepts an environment override, `SCB_<SECTION>_<KEY>`:

```bash
SCB_ANTHROPIC_EFFORT=high SCB_CONFIRMATION_TIMEOUT_SECONDS=90 python -m signal_calendar_bot run
```

The API key is **not** a config value — it comes from `ANTHROPIC_API_KEY` or a
`0600` key file. See [docs/SETUP.md](docs/SETUP.md) part 2.

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

Two systemd user units with `Restart=always` (see `systemd/`). The Ollama unit
is gone — there is no local model to keep resident any more.

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
pytest              # 75 tests, no network or API key needed
ruff check src tests
```

The scheduling engine, confirmation state machine, dedupe, sanitizer, and the
intent schema/narrowing layer are all tested without the Claude API, signal-cli,
or Google — and without spending a cent.

## Scope

**v1:** `create`, `query`, `move`, `delete`, `unknown`; primary calendar only;
Note to Self only.

**Not in v1:** recurring events, multiple calendars, attendee invitations, group
chats, proactive notifications.

**Designed for, not yet built:** morning agenda push, conflict detection on
incoming invites, gap detection.
