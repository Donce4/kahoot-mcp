"""MCP server that exposes the Kahoot bot as tools.

The server is transport agnostic (stdio by default) and is meant to be
launched by an MCP client such as OpenCode:

    python -m kahoot_mcp
"""
from __future__ import annotations

import asyncio
import json
import os
import warnings
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPDeprecationWarning
from mcp.types import ToolAnnotations

from kahoot_core.quiz import QuizClient, build_plan
from kahoot_core.sessions import GameSession, SessionManager

INSTRUCTIONS = """
Kahoot bot. Typical flow:

1. `join_game(pin, nickname)` - joins the lobby with the game pin. The quiz id
   is discovered from the game itself, so it is usually not needed.
2. `game_status()` - poll for state, score and which questions were answered.
3. If the session reports `two_factor_pending`, read the colour code off the
   host screen and call `submit_2fa(session_id, code)` (for example "rbyg").
4. `get_answers()` - the full answer sheet of the quiz.
5. `stop_game()` - leave the game.

Environment:
  KAHOOT_EMAIL / KAHOOT_PASSWORD - optional Kahoot account, used to search
  for quizzes by name and to read private quizzes.
  KAHOOT_DEFAULT_NICKNAME - fallback nickname.
"""

server = MCPServer(
    name="kahoot",
    title="Kahoot Answer Bot",
    version="1.0.0",
    instructions=INSTRUCTIONS,
)

manager = SessionManager()

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
MUTATING = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)


# --------------------------------------------------------------------- utils
def _credentials() -> tuple[Optional[str], Optional[str]]:
    return os.environ.get("KAHOOT_EMAIL"), os.environ.get("KAHOOT_PASSWORD")


def _quiz_client() -> QuizClient:
    """Build a quiz client, authenticating when credentials are available."""
    client = QuizClient()
    email, password = _credentials()
    if email and password:
        try:
            client.authenticate(email, password)
        except Exception:
            pass
    return client


def _session(session_id: Optional[str]) -> Optional[GameSession]:
    if session_id:
        return manager.get(session_id)
    return manager.latest()


def _require_session(session_id: Optional[str]) -> GameSession:
    session = _session(session_id)
    if session is None:
        raise ValueError("No game session found. Use join_game first.")
    return session


async def _notify(ctx: Optional[Context], message: str) -> None:
    """Send a log notification to the client, if it supports logging.

    The MCP logging capability is soft deprecated (SEP-2577), so failures are
    ignored and the warning is filtered out.
    """
    if ctx is None:
        return
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", MCPDeprecationWarning)
            await ctx.info(message)
    except Exception:
        pass


# --------------------------------------------------------------------- tools
@server.tool(
    description=(
        "Join a Kahoot game with the given game pin and nickname and start "
        "answering automatically. The quiz id is discovered from the game "
        "itself, so only the pin is required. Returns a session id that can be "
        "used with the other tools."
    ),
    annotations=MUTATING,
)
async def join_game(
    pin: str,
    nickname: Optional[str] = None,
    quiz_id: Optional[str] = None,
    quiz_name: Optional[str] = None,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Start a game in the background."""
    nickname = nickname or os.environ.get("KAHOOT_DEFAULT_NICKNAME") or "Player"
    session = await manager.create(
        pin=pin,
        nickname=nickname,
        quiz_id=quiz_id,
        quiz_name=quiz_name,
        quiz_client=_quiz_client(),
    )
    # give the lobby a moment so the status is meaningful right away
    await asyncio.sleep(1.0)
    await _notify(ctx, f"Joined Kahoot {pin} as {nickname} (session {session.id})")
    return session.snapshot()


@server.tool(
    description=(
        "Get the current state of a game session: lobby state, score, rank, "
        "how many questions were answered and whether a 2FA code is needed. "
        "Uses the most recent session when no id is given."
    ),
    annotations=READ_ONLY,
)
async def game_status(session_id: Optional[str] = None, ctx: Context = None) -> Dict[str, Any]:
    """Poll a running game."""
    session = _require_session(session_id)
    return session.snapshot(include_questions=True)


@server.tool(
    description=(
        "Return the answer sheet of the current quiz: every question with the "
        "correct answer and the questions the bot already answered."
    ),
    annotations=READ_ONLY,
)
async def get_answers(session_id: Optional[str] = None, ctx: Context = None) -> Dict[str, Any]:
    """Fetch the answers of the running game."""
    session = _require_session(session_id)
    data = session.snapshot()
    data["answers"] = session.plan.to_dict() if session.plan else []
    data["answered"] = session.questions
    return data


@server.tool(
    description=(
        "Submit the two factor code for a game that asked for one. Read the "
        "colour sequence off the host screen and pass it as four letters, for "
        "example 'rbyg'."
    ),
    annotations=MUTATING,
)
async def submit_2fa(session_id: Optional[str], code: str, ctx: Context = None) -> Dict[str, Any]:
    """Hand over the 2FA sequence."""
    session = _require_session(session_id)
    if session.game is None:
        raise ValueError("The session has no running game.")
    accepted = session.game.submit_2fa(code)
    await _notify(ctx, f"2FA code {'accepted' if accepted else 'rejected (no 2FA pending)'}")
    return {"accepted": accepted, "session_id": session.id}


@server.tool(
    description="Leave a running game and close the websocket connection.",
    annotations=MUTATING,
)
async def stop_game(session_id: Optional[str] = None, ctx: Context = None) -> Dict[str, Any]:
    """Stop a game."""
    session = _require_session(session_id)
    stopped = await manager.stop(session.id)
    await _notify(ctx, f"Stopped session {session.id}")
    return {"stopped": stopped, "session_id": session.id, "state": session.state}


@server.tool(
    description=(
        "Search Kahoot for quizzes by title. Needs KAHOOT_EMAIL/KAHOOT_PASSWORD "
        "for private quizzes; public quizzes work without credentials."
    ),
    annotations=READ_ONLY,
)
async def find_quiz(query: str, limit: int = 10, ctx: Context = None) -> Dict[str, Any]:
    """Search quizzes by name."""
    client = _quiz_client()
    entities = await asyncio.to_thread(client.search, query, limit)
    return {
        "query": query,
        "results": [QuizClient.summarize(entity) for entity in entities],
    }


@server.tool(
    description="Fetch a quiz by its uuid and return its answers.",
    annotations=READ_ONLY,
)
async def quiz_details(quiz_id: str, ctx: Context = None) -> Dict[str, Any]:
    """Read a quiz and its answer key."""
    client = _quiz_client()
    quiz = await asyncio.to_thread(client.fetch_quiz, quiz_id)
    plan = build_plan(quiz)
    return {"quiz_id": quiz_id, **plan.to_dict()}


@server.tool(
    description="List all game sessions started through this server.",
    annotations=READ_ONLY,
)
async def list_sessions(ctx: Context = None) -> Dict[str, Any]:
    """List known sessions."""
    return {"sessions": [session.snapshot() for session in manager.list()]}


@server.tool(
    description=(
        "Return the raw Kahoot protocol messages of a session. Useful to debug "
        "what the game sends (answer maps, question indexes, login responses)."
    ),
    annotations=READ_ONLY,
)
async def game_log(session_id: Optional[str] = None, limit: int = 50, ctx: Context = None) -> Dict[str, Any]:
    """Show the protocol log of a session."""
    session = _require_session(session_id)
    events = [event.to_dict() for event in session.events[-limit:]]
    return {
        "session_id": session.id,
        "state": session.state,
        "events": events,
        "raw": session.game.raw_log[-limit:] if session.game else [],
    }


# ----------------------------------------------------------------- resources
@server.resource("kahoot://sessions", mime_type="application/json")
async def list_sessions_resource() -> str:
    """All sessions as JSON."""
    return json.dumps([session.snapshot() for session in manager.list()], indent=2)


@server.resource("kahoot://sessions/{session_id}", mime_type="application/json")
async def session_resource(session_id: str) -> str:
    """A single session as JSON."""
    session = manager.get(session_id)
    if session is None:
        return json.dumps({"error": f"unknown session {session_id}"})
    return json.dumps(session.snapshot(include_questions=True), indent=2)


@server.resource("kahoot://sessions/{session_id}/log", mime_type="text/plain")
async def session_log_resource(session_id: str) -> str:
    """Human readable event log of a session."""
    session = manager.get(session_id)
    if session is None:
        return f"unknown session {session_id}"
    return "\n".join(session.log_lines())


@server.resource("kahoot://sessions/{session_id}/answers", mime_type="application/json")
async def session_answers_resource(session_id: str) -> str:
    """Answer sheet of a session as JSON."""
    session = manager.get(session_id)
    if session is None:
        return json.dumps({"error": f"unknown session {session_id}"})
    return json.dumps(
        {
            "quiz_id": session.quiz_id,
            "title": session.quiz_title,
            "answers": session.plan.to_dict() if session.plan else [],
            "answered": session.questions,
        },
        indent=2,
    )


def main() -> None:
    """Entry point for `python -m kahoot_mcp`."""
    server.run(transport=os.environ.get("KAHOOT_MCP_TRANSPORT", "stdio"))  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
