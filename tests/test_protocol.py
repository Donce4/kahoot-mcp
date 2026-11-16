"""Tests for the live game protocol handling (answer map, fingerprint, nicknames)."""
from __future__ import annotations

import asyncio

from kahoot_core.engine import KahootGame
from kahoot_core.quiz import build_plan

QUIZ = {
    "title": "Explorers",
    "questions": [
        {
            "question": "q0",
            "type": "quiz",
            "choices": [
                {"answer": "a", "correct": False},
                {"answer": "b", "correct": True},
            ],
        },
        {
            "question": "q1",
            "type": "quiz",
            "choices": [
                {"answer": "c", "correct": True},
                {"answer": "d", "correct": False},
            ],
        },
        {
            "question": "q2",
            "type": "quiz",
            "choices": [
                {"answer": "e", "correct": False},
                {"answer": "f", "correct": False},
                {"answer": "g", "correct": True},
            ],
        },
    ],
}


def _game(fingerprint=None, answer_map=None) -> KahootGame:
    game = KahootGame("123456", "Donatas")
    if fingerprint is not None:
        game.live_fingerprint = fingerprint
    if answer_map is not None:
        game.answer_map = answer_map
    return game


def test_fingerprint_matches_exactly():
    game = _game(fingerprint=[2, 2, 3])
    offset, exact = game._match_fingerprint(QUIZ)
    assert (offset, exact) == (0, True)


def test_fingerprint_matches_with_content_block_offset():
    # the live game starts with a content block, so the first question is #1
    game = _game(fingerprint=[2, 3])
    offset, exact = game._match_fingerprint(QUIZ)
    assert (offset, exact) == (1, True)


def test_fingerprint_does_not_match_other_quiz():
    game = _game(fingerprint=[4, 4, 4])
    offset, exact = game._match_fingerprint(QUIZ)
    assert exact is False


def test_answer_map_translates_original_index_to_screen():
    game = _game()
    game.plan = build_plan(QUIZ)
    game.quiz_offset = 0
    # question 0 -> original choice 1, but on screen it sits at position 0
    game.answer_map = {0: 1, 1: 0}
    original, source = game._resolve_choice(0, "", [])
    assert original == 1
    assert source == "index-match"
    assert game.answer_map.get(original, original) == 0


def test_offset_is_used_for_the_index_lookup():
    game = _game(fingerprint=[2, 3])
    game.plan = build_plan(QUIZ)
    game.quiz_offset = 1
    # live question 0 is quiz question 1 -> correct choice index 0
    original, source = game._resolve_choice(0, "", [])
    assert (original, source) == (0, "index-match")


def test_duplicate_nickname_is_retried():
    published = []

    class FakeClient:
        async def publish(self, channel, data):
            published.append((channel, data))

    game = _game()
    asyncio.run(game._retry_with_other_nickname(FakeClient()))
    assert game.nickname == "Donatas1"
    assert published[0][0] == "/service/controller"
    assert published[0][1]["name"] == "Donatas1"


def test_unsupported_question_types_are_skipped():
    game = _game()
    game.plan = build_plan(QUIZ)
    question = {"questionIndex": 0, "type": "open_ended"}
    asyncio.run(game._handle_start_question(question, FakeNoopClient()))
    assert game.answered == 0
    assert game.questions[0]["answered_by"] == "skipped (open_ended)"


class FakeNoopClient:
    async def publish(self, channel, data):
        return None
