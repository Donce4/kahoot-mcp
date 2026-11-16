"""Async Kahoot game engine.

The engine is transport agnostic: it owns the websocket session, the answer
plan and the game state, and reports everything through a callback.  The CLI
and the MCP server both sit on top of it.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Optional

import aiocometd
import requests

from .challenge import colors_to_sequence, solve_challenge, xor_session_token
from .quiz import ALLOWED_TYPES, AnswerPlan, QuizClient, build_plan, normalize

#: CometD message id -> name (from the Kahoot client protocol).
MESSAGE_NAMES: Dict[int, str] = {
    1: "GET_READY",
    2: "START_QUESTION",
    3: "GAME_OVER",
    4: "TIME_UP",
    5: "PLAY_AGAIN",
    6: "ANSWER_SELECTED",
    7: "ANSWER_RESPONSE",
    8: "REVEAL_ANSWER",
    9: "START_QUIZ",
    10: "RESET_CONTROLLER",
    11: "SUBMIT_FEEDBACK",
    12: "FEEDBACK",
    13: "REVEAL_RANKING",
    14: "USERNAME_ACCEPTED",
    15: "USERNAME_REJECTED",
    16: "REQUEST_RECOVERY_DATA_FROM_PLAYER",
    17: "SEND_RECOVERY_DATA_TO_CONTROLLER",
    18: "JOIN_TEAM_MEMBERS",
    19: "JOIN_TEAM_MEMBERS_RESPONSE",
    20: "START_TEAM_TALK",
    21: "SKIP_TEAM_TALK",
    31: "IFRAME_CONTROLLER_EVENT",
    32: "SERVER_IFRAME_EVENT",
    40: "STORY_BLOCK_GET_READY",
    41: "REACTION_SELECTED",
    42: "REACTION_RESPONSE",
    43: "GAME_BLOCK_START",
    44: "GAME_BLOCK_END",
    45: "GAME_BLOCK_ANSWER",
    50: "SUBMIT_TWO_FACTOR",
    51: "TWO_FACTOR_AUTH_INCORRECT",
    52: "TWO_FACTOR_AUTH_CORRECT",
    53: "RESET_TWO_FACTOR_AUTH",
}

#: Static token the Kahoot web client sends when logging into a lobby.
CAPTCHA_TOKEN = "KAHOOT_TOKEN_eyJ2ZXJzaW9uIjoiIn0="

#: Choice used when nothing better is known.
DEFAULT_ANSWER = 0

#: Minimum similarity between two answer texts to accept a fuzzy match.
FUZZY_MATCH_THRESHOLD = 0.85

#: How many nicknames to try when the wanted one is already taken.
NICKNAME_ATTEMPTS = 5

#: Dump every raw protocol message into the session log (KAHOOT_DEBUG_RAW=1).
RAW_DEBUG = os.environ.get("KAHOOT_DEBUG_RAW", "") not in ("", "0", "false")

#: How long we wait for a 2FA code before giving up.
TWO_FACTOR_TIMEOUT = 120.0


class KahootError(RuntimeError):
    """Raised when the game cannot be played."""


@dataclass
class GameEvent:
    """A single thing that happened during the game."""

    ts: float
    kind: str
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"ts": self.ts, "kind": self.kind, "data": self.data}


class KahootGame:
    """Plays a single Kahoot game."""

    def __init__(
        self,
        pin: str,
        nickname: str,
        quiz_id: Optional[str] = None,
        quiz_name: Optional[str] = None,
        quiz_client: Optional[QuizClient] = None,
        on_event: Optional[Callable[[GameEvent], None]] = None,
        two_factor_timeout: float = TWO_FACTOR_TIMEOUT,
    ):
        self.pin = str(pin)
        self.nickname = nickname
        self.quiz_id = quiz_id
        self.quiz_name = quiz_name
        self.quiz_client = quiz_client or QuizClient()
        self.on_event = on_event
        self.two_factor_timeout = two_factor_timeout

        self.state = "idle"
        self.error: Optional[str] = None
        self.session_id: Optional[str] = None
        self.quiz_title: Optional[str] = None
        self.plan: Optional[AnswerPlan] = None
        self.score = 0
        self.rank: Optional[int] = None
        self.answered = 0
        self.correct = 0
        self.questions: List[Dict[str, Any]] = []
        self.two_factor_pending = False
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None

        # live game metadata (see the Kahoot cometd protocol)
        self.answer_map: Dict[int, int] = {}
        self.live_fingerprint: List[int] = []
        self.question_index = 0
        self.quiz_offset = 0
        self.quiz_name: Optional[str] = self.quiz_name
        self.raw_log: List[str] = []
        self.cid: Optional[str] = None
        self.game_status: Optional[str] = None

        self._client: Optional[aiocometd.Client] = None
        self._stop = asyncio.Event()
        self._two_factor_code: Optional[str] = None
        self._two_factor_event = asyncio.Event()
        self._question_index = 0
        self._nickname = nickname
        self._nickname_attempts = 0

    # ------------------------------------------------------------- utilities
    def _emit(self, kind: str, data: Optional[Dict[str, Any]] = None) -> None:
        event = GameEvent(ts=time.time(), kind=kind, data=data or {})
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:  # never let a listener break the game
                pass

    def _set_state(self, state: str) -> None:
        self.state = state
        self._emit("state", {"state": state})

    # ---------------------------------------------------------------- public
    async def run(self) -> None:
        """Connect to the lobby and play until the game is over."""
        self.started_at = time.time()
        try:
            self._set_state("connecting")
            await self._resolve_session()
            await self._play()
        except asyncio.CancelledError:
            self._set_state("stopped")
            raise
        except KahootError as exc:
            self.error = str(exc)
            self._set_state("error")
        except Exception as exc:  # pragma: no cover - defensive
            self.error = f"{type(exc).__name__}: {exc}"
            self._set_state("error")

    def submit_2fa(self, code: str) -> bool:
        """Hand the two factor sequence to the running game."""
        if not self.two_factor_pending:
            return False
        self._two_factor_code = code
        self._two_factor_event.set()
        return True

    async def stop(self) -> None:
        """Stop the game and close the socket."""
        self._stop.set()
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
        if self.state not in ("finished", "error", "stopped"):
            self._set_state("stopped")

    # ----------------------------------------------------------------- steps
    async def _resolve_session(self) -> None:
        """Reserve a session for the pin and solve the anti-bot challenge."""
        url = f"https://play.kahoot.it/reserve/session/{self.pin}/?{int(time.time())}"
        response = await asyncio.to_thread(self.quiz_client.session.get, url)
        if response.status_code != 200:
            raise KahootError(
                f"Pin {self.pin} does not exist. The lobby may be closed or the game already started."
            )
        token = response.headers.get("x-kahoot-session-token")
        if not token:
            raise KahootError("Kahoot did not return a session token.")
        challenge = response.json().get("challenge")
        if not challenge:
            raise KahootError("Kahoot did not return a challenge.")
        solution = await asyncio.to_thread(solve_challenge, challenge)
        self.session_id = xor_session_token(token, solution)
        self._emit("session", {"session_id": self.session_id})

    async def _play(self) -> None:
        url = f"wss://play.kahoot.it/cometd/{self.pin}/{self.session_id}"
        async with aiocometd.Client(url, ssl=True) as client:
            self._client = client
            await client.subscribe("/service/controller")
            await client.subscribe("/service/player")
            await client.subscribe("/service/status")
            await client.publish(
                "/service/controller",
                {
                    "host": "kahoot.it",
                    "gameid": self.pin,
                    "captchaToken": CAPTCHA_TOKEN,
                    "name": self.nickname,
                    "type": "login",
                },
            )
            self._set_state("in_lobby")

            async for raw_message in client:
                message = raw_message.get("data", {})
                if "error" in message:
                    raise KahootError(message.get("description", "Unknown Kahoot error"))
                if "id" not in message:
                    # control messages: loginResponse, status, ...
                    self._handle_control(message)
                    continue
                kind = MESSAGE_NAMES.get(message["id"], str(message["id"]))
                content = message.get("content")
                data: Dict[str, Any] = {}
                if content:
                    try:
                        data = json.loads(content)
                    except (TypeError, ValueError):
                        data = {"raw": content}
                if RAW_DEBUG:
                    line = (
                        f"channel={raw_message.get('channel')} id={message['id']} "
                        f"({kind}) {json.dumps(data, ensure_ascii=False)[:1200]}"
                    )
                    self.raw_log.append(line)
                    self._emit("raw", {"id": message["id"], "kind": kind, "data": data})
                await self._handle(kind, data, client)
                if self.state in ("finished", "reset", "stopped"):
                    break
        self.finished_at = time.time()
        if self.state not in ("error", "stopped"):
            self._set_state("finished")

    async def _handle(self, kind: str, data: Dict[str, Any], client: aiocometd.Client) -> None:
        if kind == "GET_READY":
            # id 1: the game tells us which question comes next and how the
            # answers are shuffled on screen
            self._handle_question_intro(data)
            await self._try_resolve_quiz()
        elif kind == "START_QUESTION":
            # id 2: the question is live, this is the moment to answer
            await self._handle_start_question(data, client)
        elif kind == "USERNAME_ACCEPTED":
            self._set_state("joined")
        elif kind == "USERNAME_REJECTED":
            if "duplicate" in str(data.get("description", "")).lower():
                await self._retry_with_other_nickname(client)
            else:
                raise KahootError(f"Kahoot rejected the nickname '{self.nickname}'.")
        elif kind == "RESET_TWO_FACTOR_AUTH":
            await self._handle_two_factor(client)
        elif kind == "TWO_FACTOR_AUTH_CORRECT":
            self.two_factor_pending = False
            self._emit("two_factor", {"ok": True})
        elif kind == "TWO_FACTOR_AUTH_INCORRECT":
            raise KahootError("The 2FA code was incorrect.")
        elif kind == "START_QUIZ":
            await self._handle_start_quiz(data)
        elif kind == "ANSWER_RESPONSE":
            self._handle_answer_response(data)
        elif kind == "GAME_OVER":
            self._handle_game_over(data)
        elif kind == "RESET_CONTROLLER":
            self._set_state("reset")
        else:
            self._emit("message", {"kind": kind, "data": data})

    # ------------------------------------------------------------- nickname
    def _handle_control(self, message: Dict[str, Any]) -> None:
        """Handle the control messages the server sends without a message id."""
        kind = message.get("type")
        if kind == "loginResponse":
            # the server hands out the client id that identifies the player
            cid = message.get("cid")
            if cid:
                self.cid = str(cid)
            self._emit("login_response", {"cid": self.cid})
        elif kind == "status":
            self.game_status = message.get("status")
            self._emit("game_status", {"status": self.game_status})

    async def _retry_with_other_nickname(self, client: aiocometd.Client) -> None:
        """The wanted nickname is taken, join with a numbered variant."""
        self._nickname_attempts += 1
        if self._nickname_attempts > NICKNAME_ATTEMPTS:
            raise KahootError(
                f"The nickname '{self.nickname}' is taken and no free variant was found."
            )
        candidate = f"{self.nickname}{self._nickname_attempts}"
        self.nickname = candidate
        self._emit("nickname_changed", {"nickname": candidate})
        payload: Dict[str, Any] = {
            "host": "kahoot.it",
            "gameid": self.pin,
            "captchaToken": CAPTCHA_TOKEN,
            "name": candidate,
            "type": "login",
        }
        if self.cid:
            payload["cid"] = self.cid
        await client.publish("/service/controller", payload)

    # -------------------------------------------------------------- question
    def _handle_question_intro(self, data: Dict[str, Any]) -> None:
        """Store the per question metadata Kahoot sends before a question."""
        answer_map = data.get("answerMap")
        if isinstance(answer_map, dict):
            self.answer_map = {int(key): int(value) for key, value in answer_map.items()}
        fingerprint = data.get("quizQuestionAnswers")
        if isinstance(fingerprint, list) and fingerprint:
            try:
                self.live_fingerprint = [int(value) for value in fingerprint]
            except (TypeError, ValueError):
                pass
        question_index = data.get("questionIndex")
        if isinstance(question_index, int):
            self.question_index = question_index
        self._emit(
            "question_intro",
            {
                "question_index": self.question_index,
                "answer_map": self.answer_map,
                "choices": data.get("numberOfChoices"),
                "type": data.get("type") or data.get("gameBlockType"),
            },
        )

    # ------------------------------------------------------------- 2 factor
    async def _handle_two_factor(self, client: aiocometd.Client) -> None:
        self.two_factor_pending = True
        self._two_factor_code = None
        self._two_factor_event = asyncio.Event()
        self._set_state("awaiting_2fa")
        self._emit("two_factor_required", {"pin": self.pin})
        try:
            await asyncio.wait_for(self._two_factor_event.wait(), timeout=self.two_factor_timeout)
        except asyncio.TimeoutError:
            raise KahootError("No 2FA code was submitted in time.") from None
        sequence = colors_to_sequence(self._two_factor_code or "")
        await client.publish(
            "/service/controller",
            {
                "content": json.dumps({"sequence": sequence}),
                "gameid": self.pin,
                "host": "kahoot.it",
                "type": "message",
                "id": 50,
            },
        )
        self.two_factor_pending = False

    # ----------------------------------------------------------------- quiz
    async def _handle_start_quiz(self, data: Dict[str, Any]) -> None:
        """The quiz is starting: remember its name and preload the answers."""
        quiz_name = data.get("quizName")
        if quiz_name and not self.quiz_name:
            self.quiz_name = quiz_name
            self._emit("quiz_name", {"quiz_name": quiz_name})
        fingerprint = data.get("quizQuestionAnswers")
        if isinstance(fingerprint, list) and fingerprint:
            try:
                self.live_fingerprint = [int(value) for value in fingerprint]
            except (TypeError, ValueError):
                pass
        await self._try_resolve_quiz()

    async def _try_resolve_quiz(self) -> None:
        """Find the quiz of the running game and load its answers.

        The player protocol does not hand out the quiz uuid, so the quiz is
        identified by its title plus the per question choice counts Kahoot
        sends with every question (``quizQuestionAnswers``).
        """
        if self.plan is not None:
            return
        if self.quiz_id:
            try:
                quiz = await asyncio.to_thread(self.quiz_client.fetch_quiz, self.quiz_id)
            except Exception as exc:
                self._emit("answers_error", {"message": str(exc)})
                return
            offset, exact = self._match_fingerprint(quiz)
            self._apply_plan(quiz, offset, exact)
            return
        if not self.quiz_name:
            return
        try:
            entities = await asyncio.to_thread(self.quiz_client.search, self.quiz_name, 25)
        except Exception as exc:
            self._emit("answers_error", {"message": str(exc)})
            return
        fallback = None
        for entity in entities:
            summary = QuizClient.summarize(entity)
            if not summary.get("quiz_id"):
                continue
            try:
                quiz = await asyncio.to_thread(self.quiz_client.fetch_quiz, summary["quiz_id"])
            except Exception:
                continue
            offset, exact = self._match_fingerprint(quiz)
            if exact:
                self.quiz_id = summary["quiz_id"]
                self._emit("quiz_id", {"quiz_id": self.quiz_id})
                self._apply_plan(quiz, offset, exact)
                return
            if fallback is None:
                fallback = (quiz, offset, summary["quiz_id"])
        if fallback is not None and not self.live_fingerprint:
            quiz, offset, quiz_id = fallback
            self.quiz_id = quiz_id
            self._emit("quiz_id", {"quiz_id": quiz_id})
            self._apply_plan(quiz, offset, False)
            return
        self._emit(
            "answers_error",
            {
                "message": (
                    f"No public quiz named '{self.quiz_name}' matches this game. "
                    "Pass the quiz id to join a private quiz."
                )
            },
        )

    def _match_fingerprint(self, quiz: Dict[str, Any]) -> tuple:
        """Match a quiz against the live game by its per question choice counts.

        Returns the offset between the live question index and the index in
        the downloaded quiz, plus whether the match was exact.
        """
        counts = [
            len(question.get("choices", []) or [])
            for question in quiz.get("questions", []) or []
        ]
        live = self.live_fingerprint
        if not live:
            return 0, False
        for offset in range(-len(live) + 1, len(counts)):
            matched = True
            for index, value in enumerate(live):
                position = index + offset
                if position < 0 or position >= len(counts) or counts[position] != value:
                    matched = False
                    break
            if matched:
                return offset, True
        return 0, False

    def _apply_plan(self, quiz: Dict[str, Any], offset: int = 0, exact: bool = False) -> None:
        self.plan = build_plan(quiz)
        self.quiz_title = self.plan.title or quiz.get("title")
        self.quiz_offset = offset
        self._emit(
            "answers_ready",
            {
                "count": len(self.plan.items),
                "title": self.quiz_title,
                "quiz_id": self.quiz_id,
                "offset": offset,
                "verified": exact,
            },
        )

    # ------------------------------------------------------------- question
    async def _handle_start_question(self, data: Dict[str, Any], client: aiocometd.Client) -> None:
        index = int(data.get("questionIndex", self._question_index))
        self._question_index = index + 1
        question_text = data.get("question", "") or ""
        question_type = data.get("type") or data.get("gameBlockType") or "quiz"
        choices = data.get("choices", []) or []

        # the answer map of the running question may also arrive with it
        answer_map = data.get("answerMap")
        if isinstance(answer_map, dict):
            self.answer_map = {int(key): int(value) for key, value in answer_map.items()}

        if question_type not in ALLOWED_TYPES:
            # jumble / open_ended need a typed answer, a choice index is useless
            self.questions.append(
                {
                    "index": index,
                    "question": question_text,
                    "choice_index": None,
                    "answered_by": f"skipped ({question_type})",
                    "ts": time.time(),
                }
            )
            self._emit(
                "skipped",
                {"index": index, "question": question_text, "type": question_type},
            )
            return

        original, source = self._resolve_choice(index, question_text, choices)
        # the answer map translates the original choice order into the order
        # the options are shown in on screen
        onscreen = self.answer_map.get(original, original)
        await self._send_answer(client, onscreen)
        self.answered += 1
        self.questions.append(
            {
                "index": index,
                "question": question_text,
                "choice_index": onscreen,
                "original_choice": original,
                "answered_by": source,
                "ts": time.time(),
            }
        )
        self._emit(
            "answered",
            {
                "index": index,
                "question": question_text,
                "choice_index": onscreen,
                "original_choice": original,
                "answered_by": source,
            },
        )

    def _resolve_choice(self, index: int, question_text: str, choices: List[Dict[str, Any]]) -> tuple:
        """Pick the correct choice in the original (unshuffled) order.

        Text matching is preferred when the server sends the question text,
        otherwise the question index is used (adjusted by the offset that was
        found while matching the live game against the downloaded quiz).
        """
        if self.plan is not None:
            item = self.plan.by_question.get(normalize(question_text))
            if item is not None:
                wanted = normalize(item.answer)
                best_index = None
                best_score = 0.0
                for choice_index, choice in enumerate(choices):
                    text = normalize(self._choice_text(choice))
                    if text == wanted:
                        return choice_index, "text-match"
                    score = SequenceMatcher(None, text, wanted).ratio()
                    if score > best_score:
                        best_score = score
                        best_index = choice_index
                if best_index is not None and best_score >= FUZZY_MATCH_THRESHOLD:
                    return best_index, "fuzzy-match"
                return item.choice_index, "plan-index"
            by_index = self.plan.by_index.get(index + self.quiz_offset)
            if by_index is not None:
                return by_index.choice_index, "index-match"
        return DEFAULT_ANSWER, "fallback"

    @staticmethod
    def _choice_text(choice: Any) -> str:
        """Extract the text of a live answer option."""
        if isinstance(choice, str):
            return choice
        if isinstance(choice, dict):
            return choice.get("text") or choice.get("answer") or ""
        return ""

    async def _send_answer(self, client: aiocometd.Client, choice_index: int) -> None:
        content = json.dumps(
            {
                "choice": choice_index,
                "meta": {
                    "lag": 0,
                    "device": {"userAgent": "kbot", "screen": {"width": 1920, "height": 1080}},
                },
            }
        )
        payload: Dict[str, Any] = {
            "content": content,
            "gameid": self.pin,
            "host": "kahoot.it",
            "type": "message",
            "id": 45,
        }
        if self.cid:
            payload["cid"] = self.cid
        await client.publish("/service/controller", payload)

    # -------------------------------------------------------------- results
    def _handle_answer_response(self, data: Dict[str, Any]) -> None:
        points = data.get("points")
        if isinstance(points, (int, float)):
            self.score = int(points)
        correct = data.get("correct")
        if correct:
            self.correct += 1
        self._emit("score", {"score": self.score, "correct": self.correct})

    def _handle_game_over(self, data: Dict[str, Any]) -> None:
        rank = data.get("rank")
        if isinstance(rank, int):
            self.rank = rank
        points = data.get("points")
        if isinstance(points, (int, float)):
            self.score = int(points)
        # the quiz uuid is only revealed when the game ends, remember it so
        # the answer sheet can still be fetched afterwards
        quiz_id = data.get("quizID") or data.get("quizId")
        if quiz_id and not self.quiz_id:
            self.quiz_id = quiz_id
            self._emit("quiz_id", {"quiz_id": quiz_id})
        self._set_state("finished")
