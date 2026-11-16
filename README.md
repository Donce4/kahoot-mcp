# Kahoot Answer Bot

A bot that takes the name or ID of a Kahoot and gets a perfect score with the nickname of your choosing.
It is available as a **CLI** and as an **MCP server**, so an AI agent (OpenCode, Copilot, Claude, ...)
can join a game and answer it for you.

*This project was started by [reteps](https://github.com/reteps/kbot) but it had been broken for quite a
while, so [Raymo111](https://github.com/Raymo111) forked it and made it work. This version refactors the
bot into a reusable core and adds an MCP server.*

## Features

- Joins a Kahoot as a regular player and answers every question
- Discovers the quiz on its own: the game pin is enough, the quiz UUID is read from the game
- Fast: the answer sheet is downloaded **before** the first question, answering is a dictionary lookup
- Robust against shuffling: answers are matched by **question text**, not by position
- 2FA compatible
- Search by quiz name or ID (Kahoot account optional)
- MCP server with `join_game`, `game_status`, `get_answers`, `submit_2fa`, `stop_game`, `find_quiz`,
  `quiz_details` and `list_sessions`

## Requirements

- Python **3.12+** (the vendored `aiocometd` is patched for modern Python, see `vendor/`)
- [uv](https://docs.astral.sh/uv/) is recommended, but `pip` works too

## Installation

```sh
git clone https://github.com/Raymo111/kahoot-answer-bot.git
cd kahoot-answer-bot

# with uv
uv venv --python 3.12
uv pip install -r requirements.txt
uv pip install -e .          # optional: installs the `kahoot-bot` and `kahoot-mcp` entry points

# or with pip inside a virtualenv
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## CLI usage

```sh
source .venv/bin/activate

# join a game (the quiz id is optional, it is discovered from the game)
python kbot --nick Donatas --pin 461444

# ... or with an explicit quiz id
python kbot --nick Donatas --pin 461444 --quizID 17821f2b-4e7d-45fc-9e90-3fbca6c62973

# look a quiz up and print the answers without joining
python kbot --search --email you@example.com --password 'secret' --quizName "Explorers"

python kbot --help
```

| Option | Meaning |
| --- | --- |
| `-e, --email` | Email used to log in to create.kahoot.it |
| `-a, --password` | The matching password |
| `-n, --nick` | Nickname to join with |
| `-p, --pin` | The game pin |
| `-s, --search` | Search for a quiz instead of joining one |
| `-q, --quizName` | Title of the quiz |
| `-i, --quizID` | UUID of the quiz |
| `-m, --maxCount` | How many quizzes to list when searching |
| `-d, --debug` | Verbose output |

## MCP server

The MCP server exposes the bot as tools over stdio, so any MCP client can drive it.

```sh
source .venv/bin/activate
python -m kahoot_mcp            # stdio (default)
KAHOOT_MCP_TRANSPORT=streamable-http python -m kahoot_mcp   # optional: HTTP transport
```

### Configure OpenCode

```sh
opencode mcp add kahoot -- "$PWD/.venv/bin/python" -m kahoot_mcp
opencode mcp list
# ✓ kahoot  connected
```

Or add it to `opencode.json` by hand:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "kahoot": {
        "type": "local",
        "command": ["/absolute/path/to/kahoot-answer-bot/.venv/bin/python", "-m", "kahoot_mcp"],
        "environment": {
          "KAHOOT_EMAIL": "{env:KAHOOT_EMAIL}",
          "KAHOOT_PASSWORD": "{env:KAHOOT_PASSWORD}"
        }
      }
    }
  }
}
```

### Tools

| Tool | Arguments | What it does |
| --- | --- | --- |
| `join_game` | `pin`, `nickname?`, `quiz_id?`, `quiz_name?` | Joins the lobby and starts answering in the background, returns a `session_id` |
| `game_status` | `session_id?` | State, score, rank, answered questions, whether a 2FA code is pending |
| `get_answers` | `session_id?` | Full answer sheet of the quiz |
| `submit_2fa` | `session_id`, `code` | Submits the colour code shown by the host (`"rbyg"`) |
| `stop_game` | `session_id?` | Leaves the game |
| `find_quiz` | `query`, `limit?` | Searches quizzes by title |
| `quiz_details` | `quiz_id` | Quiz metadata and answer key |
| `list_sessions` | – | All sessions of this server |
| `game_log` | `session_id?`, `limit?` | Raw Kahoot protocol messages of a session (debugging) |

Resources: `kahoot://sessions`, `kahoot://sessions/{id}`, `kahoot://sessions/{id}/log`,
`kahoot://sessions/{id}/answers`.

Typical agent flow:

1. `join_game(pin=461444, nickname="Donatas")`
2. poll `game_status()` until the state is `awaiting_2fa` or `finished`
3. if `two_factor_pending` is true, read the colours off the host screen and call `submit_2fa(...)`
4. read the final score with `game_status()`

### Environment variables

| Variable | Purpose |
| --- | --- |
| `KAHOOT_EMAIL`, `KAHOOT_PASSWORD` | Kahoot account, needed for searching by name and for private quizzes |
| `KAHOOT_DEFAULT_NICKNAME` | Fallback nickname |
| `KAHOOT_COMETD_TRANSPORT` | `websocket` (default) or `long-polling` |
| `KAHOOT_MCP_TRANSPORT` | `stdio` (default), `sse` or `streamable-http` |
| `KAHOOT_DEBUG_RAW` | `1` logs every raw protocol message into the session log |

Credentials are read from the environment only, they are never part of a tool schema.

## How the bot answers

1. `GET kahoot.it/reserve/session/{pin}` returns a session token and a small JavaScript challenge.
   The challenge is solved with the V8 engine from `py_mini_racer`.
2. The bot connects to `wss://play.kahoot.it/cometd/{pin}/{session}` and logs in as a player.
   If the nickname is already taken it retries with `Nickname2`, `Nickname3`, ...
3. Kahoot does **not** tell players which quiz is being played. The bot identifies it by the title
   (`quizName` from the start message) plus a fingerprint: Kahoot sends the number of options per
   question, and the bot only accepts a downloaded quiz whose option counts match the live game.
   A quiz id can always be passed explicitly for private quizzes.
4. For every question Kahoot sends an `answerMap` that translates the original option order into the
   order shown on screen. The bot answers the original correct option through that map, so it also
   works when the host shuffles the options.
5. The answer is published over the already open websocket, so the latency is a few milliseconds.

Question types that need a typed answer (`open_ended`) or an ordering (`jumble`) are skipped, they are
reported in the session log.

## Caveats

Does not work when:

- Kahoot is private and no account is configured
- The questions are not available through the REST API before the game starts

## Project layout

```
kahoot_core/       shared engine (challenge solver, quiz access, game engine, sessions)
kahoot_mcp/        MCP server (tools + resources)
vendor/aiocometd/  vendored + patched aiocometd (Python 3.10+ support, websocket first)
kbot               CLI entry point
tests/             unit + end to end MCP tests
```

Run the tests with:

```sh
uv pip install pytest anyio
.venv/bin/python -m pytest tests -q
```

## Contributors

- [Raymo111](https://github.com/Raymo111) - fixing it, adding 2FA and search by ID
- [reteps](https://github.com/reteps/kbot) - main programming
- idiidk - for the challenge decoding
