"""Session management for the MCP server.

A session is one running (or finished) game.  The manager owns the asyncio
task of every game so that tools can start a game, poll it and stop it.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .engine import GameEvent, KahootGame
from .quiz import AnswerPlan, QuizClient


@dataclass
class GameSession:
    """State of a single Kahoot game."""

    id: str
    pin: str
    nickname: str
    quiz_id: Optional[str] = None
    quiz_name: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    state: str = "starting"
    error: Optional[str] = None
    quiz_title: Optional[str] = None
    plan: Optional[AnswerPlan] = None
    questions: List[Dict[str, Any]] = field(default_factory=list)
    events: List[GameEvent] = field(default_factory=list)
    score: int = 0
    rank: Optional[int] = None
    answered: int = 0
    correct: int = 0
    two_factor_pending: bool = False
    game: Optional[KahootGame] = None
    task: Optional[asyncio.Task] = None

    def snapshot(self, include_questions: bool = False) -> Dict[str, Any]:
        """Serialisable view of the session."""
        data: Dict[str, Any] = {
            "session_id": self.id,
            "pin": self.pin,
            "nickname": self.nickname,
            "state": self.state,
            "quiz_id": self.quiz_id,
            "quiz_title": self.quiz_title,
            "answers_loaded": self.plan is not None,
            "answer_count": len(self.plan.items) if self.plan else 0,
            "questions_answered": self.answered,
            "correct": self.correct,
            "score": self.score,
            "rank": self.rank,
            "two_factor_pending": self.two_factor_pending,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": time.time(),
        }
        if include_questions:
            data["questions"] = self.questions
            data["answers"] = self.plan.to_dict()["items"] if self.plan else []
        return data

    def log_lines(self, limit: int = 100) -> List[str]:
        return [
            f"{time.strftime('%H:%M:%S', time.localtime(event.ts))} {event.kind} {event.data}"
            for event in self.events[-limit:]
        ]


class SessionManager:
    """Creates and tracks :class:`GameSession` objects."""

    def __init__(self, quiz_client_factory: Optional[Callable[[], QuizClient]] = None):
        self._sessions: Dict[str, GameSession] = {}
        self._quiz_client_factory = quiz_client_factory or QuizClient

    # ------------------------------------------------------------- accessors
    def get(self, session_id: str) -> Optional[GameSession]:
        return self._sessions.get(session_id)

    def list(self) -> List[GameSession]:
        return sorted(self._sessions.values(), key=lambda session: session.created_at, reverse=True)

    def latest(self) -> Optional[GameSession]:
        sessions = self.list()
        return sessions[0] if sessions else None

    # --------------------------------------------------------------- actions
    async def create(
        self,
        pin: str,
        nickname: str,
        quiz_id: Optional[str] = None,
        quiz_name: Optional[str] = None,
        quiz_client: Optional[QuizClient] = None,
    ) -> GameSession:
        """Start a new game in the background and return its session."""
        session_id = uuid.uuid4().hex[:8]
        session = GameSession(
            id=session_id,
            pin=str(pin),
            nickname=nickname,
            quiz_id=quiz_id,
            quiz_name=quiz_name,
        )
        self._sessions[session_id] = session

        def on_event(event: GameEvent) -> None:
            self._apply_event(session, event)

        game = KahootGame(
            pin=pin,
            nickname=nickname,
            quiz_id=quiz_id,
            quiz_name=quiz_name,
            quiz_client=quiz_client or self._quiz_client_factory(),
            on_event=on_event,
        )
        session.game = game
        session.task = asyncio.create_task(game.run(), name=f"kahoot-{session_id}")
        return session

    def _apply_event(self, session: GameSession, event: GameEvent) -> None:
        session.events.append(event)
        data = event.data
        if event.kind == "state":
            session.state = data.get("state", session.state)
        elif event.kind == "error":
            session.error = data.get("message")
            session.state = "error"
        elif event.kind == "quiz_id":
            session.quiz_id = data.get("quiz_id", session.quiz_id)
        elif event.kind == "answers_ready":
            session.plan = session.game.plan if session.game else None
            session.quiz_title = data.get("title", session.quiz_title)
        elif event.kind == "answered":
            session.answered = max(session.answered, len(session.questions))
        elif event.kind == "score":
            session.score = data.get("score", session.score)
            session.correct = data.get("correct", session.correct)
        elif event.kind == "two_factor_required":
            session.two_factor_pending = True
        elif event.kind == "two_factor":
            session.two_factor_pending = False
        if session.game is not None:
            session.state = session.game.state
            session.error = session.game.error or session.error
            session.quiz_id = session.game.quiz_id or session.quiz_id
            session.quiz_title = session.game.quiz_title or session.quiz_title
            session.plan = session.game.plan or session.plan
            session.score = session.game.score or session.score
            session.rank = session.game.rank or session.rank
            session.answered = session.game.answered or session.answered
            session.correct = session.game.correct or session.correct
            session.questions = session.game.questions or session.questions

    async def stop(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session is None:
            return False
        if session.task is not None and not session.task.done():
            session.task.cancel()
            try:
                await session.task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if session.game is not None:
            await session.game.stop()
        session.state = "stopped"
        return True

    async def shutdown(self) -> None:
        for session in list(self._sessions.values()):
            await self.stop(session.id)
