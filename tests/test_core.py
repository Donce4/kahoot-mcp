"""Unit tests for the parts of the core that do not need the network."""
from __future__ import annotations

import base64

from kahoot_core.challenge import colors_to_sequence, xor_session_token
from kahoot_core.quiz import build_plan, normalize


def test_normalize_strips_and_lowercases():
    assert normalize("  Hello   World  ") == "hello world"
    assert normalize(None) == ""


def test_build_plan_extracts_correct_choices():
    quiz = {
        "title": "Test quiz",
        "creator_username": "tester",
        "questions": [
            {
                "question": "Capital of France?",
                "type": "quiz",
                "choices": [
                    {"answer": "Berlin", "correct": False},
                    {"answer": "Paris", "correct": True},
                ],
            },
            {
                "question": "Unsupported type",
                "type": "word_cloud",
                "choices": [{"answer": "x", "correct": True}],
            },
        ],
    }
    plan = build_plan(quiz)
    assert plan.title == "Test quiz"
    assert len(plan.items) == 1
    item = plan.items[0]
    assert item.answer == "Paris"
    assert item.choice_index == 1
    assert plan.by_question[normalize("capital of france?")] is item


def test_colors_to_sequence():
    assert colors_to_sequence("rbyg") == "0123"
    assert colors_to_sequence("RBYG") == "0123"
    assert colors_to_sequence("01") == "01"


def test_xor_session_token_roundtrip():
    token = base64.b64encode(b"session-token-bytes").decode()
    solution = "abcd"
    result = xor_session_token(token, solution)
    assert len(result) == len("session-token-bytes")
    # applying the same xor twice gives the original back
    assert xor_session_token(base64.b64encode(result.encode()).decode(), solution) == "session-token-bytes"
