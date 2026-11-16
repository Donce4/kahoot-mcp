"""Core Kahoot engine shared by the CLI and the MCP server."""
from .challenge import colors_to_sequence, solve_challenge, xor_session_token
from .engine import GameEvent, KahootError, KahootGame
from .quiz import AnswerItem, AnswerPlan, QuizClient, build_plan, normalize
from .sessions import GameSession, SessionManager

__all__ = [
    "AnswerItem",
    "AnswerPlan",
    "GameEvent",
    "GameSession",
    "KahootError",
    "KahootGame",
    "QuizClient",
    "SessionManager",
    "build_plan",
    "colors_to_sequence",
    "normalize",
    "solve_challenge",
    "xor_session_token",
]
