"""Read quizzes from the Kahoot REST API and build an answer plan.

The plan is the piece that makes the bot fast: it is fetched *once*, before the
first question, so that answering is a dictionary lookup instead of a HTTP
round trip.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

CREATE_BASE = "https://create.kahoot.it"

#: Question types the bot knows how to answer.
ALLOWED_TYPES = ("quiz", "multiple_select_quiz")


def normalize(text: Optional[str]) -> str:
    """Normalise question/answer text so it can be compared reliably."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass
class AnswerItem:
    """A single question with its correct choice."""

    index: int
    question: str
    answer: str
    choice_index: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "question": self.question,
            "answer": self.answer,
            "choice_index": self.choice_index,
        }


@dataclass
class AnswerPlan:
    """All answers of a quiz plus lookup tables by question text and index."""

    title: str = ""
    creator: str = ""
    items: List[AnswerItem] = field(default_factory=list)
    by_question: Dict[str, AnswerItem] = field(default_factory=dict)
    by_index: Dict[int, AnswerItem] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "creator": self.creator,
            "count": len(self.items),
            "items": [item.to_dict() for item in self.items],
        }


def build_plan(quiz: Dict[str, Any]) -> AnswerPlan:
    """Turn a raw quiz payload into an :class:`AnswerPlan`."""
    plan = AnswerPlan(
        title=quiz.get("title", ""),
        creator=quiz.get("creator_username", "") or quiz.get("creator", ""),
    )
    for index, question in enumerate(quiz.get("questions", []) or []):
        if question.get("type") not in ALLOWED_TYPES:
            continue
        choice_index = None
        answer = ""
        for choice_index_candidate, choice in enumerate(question.get("choices", []) or []):
            if choice.get("correct"):
                choice_index = choice_index_candidate
                answer = choice.get("answer", "")
                break
        if choice_index is None:
            continue
        item = AnswerItem(
            index=index,
            question=question.get("question", ""),
            answer=answer,
            choice_index=choice_index,
        )
        plan.items.append(item)
        plan.by_question[normalize(item.question)] = item
        plan.by_index[index] = item
    return plan


class QuizClient:
    """Thin wrapper around the ``create.kahoot.it`` REST endpoints."""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.auth_token: Optional[str] = None

    # ------------------------------------------------------------------ auth
    def authenticate(self, email: str, password: str) -> str:
        """Log in to create.kahoot.it and keep the bearer token."""
        response = self.session.post(
            f"{CREATE_BASE}/rest/authenticate",
            json={"username": email, "password": password, "grant_type": "password"},
            headers={"Content-Type": "application/json", "x-kahoot-login-gate": "enabled"},
        )
        if response.status_code == 401:
            raise RuntimeError("Invalid email or password.")
        if response.status_code != 200:
            raise RuntimeError(f"Login failed with status {response.status_code}")
        self.auth_token = response.json()["access_token"]
        return self.auth_token

    def _headers(self) -> Dict[str, str]:
        if self.auth_token:
            return {"Authorization": f"Bearer {self.auth_token}"}
        return {}

    # ------------------------------------------------------------------ data
    def fetch_quiz(self, quiz_id: str) -> Dict[str, Any]:
        """Fetch a quiz by its UUID."""
        response = self.session.get(f"{CREATE_BASE}/rest/kahoots/{quiz_id}", headers=self._headers())
        if response.status_code == 400:
            raise RuntimeError("Invalid quiz id (UUID).")
        if response.status_code == 404:
            raise RuntimeError("Quiz not found (it may be private).")
        if response.status_code != 200:
            raise RuntimeError(f"Could not fetch the quiz (status {response.status_code}).")
        return response.json()

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search public quizzes by title."""
        params = {
            "query": query,
            "cursor": 0,
            "limit": limit,
            "topics": "",
            "grades": "",
            "orderBy": "relevance",
            "searchCluster": 1,
            "includeExtendedCounters": False,
        }
        response = self.session.get(f"{CREATE_BASE}/rest/kahoots/", params=params, headers=self._headers())
        if response.status_code != 200:
            raise RuntimeError(f"Search failed (status {response.status_code}).")
        return response.json().get("entities", []) or []

    @staticmethod
    def summarize(entity: Dict[str, Any]) -> Dict[str, Any]:
        """Reduce a search entity to the interesting bits."""
        card = entity.get("card", {}) or {}
        return {
            "quiz_id": card.get("uuid"),
            "title": card.get("title"),
            "creator": card.get("creator_username"),
            "num_questions": card.get("number_of_questions"),
            "created": card.get("created"),
        }
