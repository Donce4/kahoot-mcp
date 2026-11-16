"""Solve the Kahoot anti-bot challenge.

Kahoot answers the ``/reserve/session/{pin}`` endpoint with a tiny JavaScript
snippet that shuffles a string.  We evaluate it with the V8 engine bundled in
``py_mini_racer`` and XOR the result with the session token.
"""
from __future__ import annotations

import base64
import re
from typing import List, Optional

from py_mini_racer import py_mini_racer

_ENGINE: Optional[py_mini_racer.MiniRacer] = None

#: The colours Kahoot uses for the two factor challenge, in index order.
COLOR_INDEX = {"r": 0, "b": 1, "y": 2, "g": 3}


def _engine() -> py_mini_racer.MiniRacer:
    """Return a (cached) V8 engine instance.

    Starting a MiniRacer costs ~30ms, and the challenge is solved for every
    session, so the instance is kept around.
    """
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = py_mini_racer.MiniRacer()
    return _ENGINE


def solve_challenge(challenge: str) -> str:
    """Evaluate the Kahoot challenge and return the shuffle key."""
    text = challenge.replace("\t", "", -1).encode("ascii", "ignore").decode("utf-8")
    parts = re.split("[{};]", text)
    if len(parts) < 8:
        raise ValueError("Unexpected challenge format")
    replace_function = "return message.replace(/./g, function(char, position) {"
    rebuilt = [
        parts[1] + "{",
        parts[2] + ";",
        replace_function,
        parts[7] + ";})};",
        parts[0],
    ]
    return _engine().eval("".join(rebuilt))


def xor_session_token(session_token: str, solution: str) -> str:
    """Derive the CometD session id from the session token and the key."""
    decoded = base64.b64decode(session_token).decode("utf-8", "strict")
    solution_chars: List[int] = [ord(char) for char in solution]
    session_chars: List[int] = [ord(char) for char in decoded]
    return "".join(
        chr(session_chars[index] ^ solution_chars[index % len(solution_chars)])
        for index in range(len(session_chars))
    )


def colors_to_sequence(code: str) -> str:
    """Convert a colour sequence such as ``rbyg`` into Kahoot's format."""
    converted = []
    for char in code.strip().lower():
        if char in COLOR_INDEX:
            converted.append(str(COLOR_INDEX[char]))
        elif char.isdigit():
            converted.append(char)
    return "".join(converted)
