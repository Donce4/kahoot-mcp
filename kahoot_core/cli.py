#!/usr/bin/env python3
"""Command line interface for the Kahoot answer bot.

The heavy lifting lives in ``kahoot_core``; this file only parses arguments
and prints what happens.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from typing import Optional

from kahoot_core.quiz import QuizClient, build_plan
from kahoot_core.sessions import SessionManager

UUID_PATTERN = re.compile(
    r"(([a-zA-Z0-9]){8}(-([a-zA-Z0-9]){4}){3}-([a-zA-Z0-9]){12})"
)


def check_id(quiz_id: str) -> str:
    """Validate that the given string looks like a Kahoot quiz uuid."""
    match = UUID_PATTERN.search(quiz_id)
    if not match:
        raise ValueError(
            "Invalid quiz id. It must have the form x8-x4-x4-x4-x12, "
            "for example 3f2a1b4c-5d6e-7f80-9a1b-2c3d4e5f6a7b"
        )
    return match.group(1)


def name_or_id() -> bool:
    """Ask the user whether to look the quiz up by name or by id."""
    while True:
        reply = str(input("quizName or quizID? [n/i] > ")).lower().strip()
        if reply[:1] == "n":
            return True
        if reply[:1] == "i":
            return False


async def run_search(quiz_name: Optional[str], quiz_id: Optional[str], client: QuizClient) -> int:
    """Print the answers of a quiz without joining a game."""
    if quiz_id:
        quiz = await asyncio.to_thread(client.fetch_quiz, check_id(quiz_id))
        plan = build_plan(quiz)
    else:
        entities = await asyncio.to_thread(client.search, quiz_name or "", 20)
        if not entities:
            print("No quizzes found.")
            return 1
        plan = None
        for entity in entities:
            summary = QuizClient.summarize(entity)
            answer = str(input(f"Check '{summary['title']}'? [y/N] ")).lower()
            if not re.match(r"y(es)?", answer):
                continue
            quiz = await asyncio.to_thread(client.fetch_quiz, summary["quiz_id"])
            plan = build_plan(quiz)
            break
        if plan is None:
            print("No quiz selected.")
            return 1

    print(f"Title: {plan.title}")
    print(f"Creator: {plan.creator}")
    for item in plan.items:
        print(f"{item.question}\n\t{item.answer}")
    return 0


async def run_game(
    pin: str,
    nickname: str,
    quiz_id: Optional[str],
    quiz_name: Optional[str],
    client: QuizClient,
) -> int:
    """Join a game and answer until it is over."""
    manager = SessionManager()
    session = await manager.create(
        pin=pin,
        nickname=nickname,
        quiz_id=check_id(quiz_id) if quiz_id else None,
        quiz_name=quiz_name,
        quiz_client=client,
    )

    def on_event(event) -> None:
        data = event.data
        if event.kind == "state":
            print(f"-> {data['state']}")
        elif event.kind == "answers_ready":
            print(f"ANSWERS RECEIVED ({data.get('count', 0)} questions)")
        elif event.kind == "answered":
            print(f"------ {data['index'] + 1} ------")
            print(f"SELECTED {data['choice_index']} ({data['answered_by']})")
        elif event.kind == "two_factor_required":
            code = input("2fa (e.g. rbyg, yrgb) > ")
            if session.game is not None:
                session.game.submit_2fa(code)
        elif event.kind == "score":
            print(f"SCORE {data['score']}")
        elif event.kind == "error":
            print(f"ERROR: {data['message']}")

    if session.game is not None:
        session.game.on_event = on_event

    try:
        await session.task
    except asyncio.CancelledError:
        pass
    finally:
        await manager.shutdown()

    if session.error:
        print(f"ERROR: {session.error}")
        return 1
    print(f"Final score: {session.score}" + (f" (rank {session.rank})" if session.rank else ""))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Kahoot answer bot")
    parser.add_argument("-e", "--email", help="The email used to login to create.kahoot.it")
    parser.add_argument("-a", "--password", help="The corresponding password used to login")
    parser.add_argument("-n", "--nick", help="The nickname to join the Kahoot with")
    parser.add_argument("-p", "--pin", help="The game pin")
    parser.add_argument("-s", "--search", action="store_true", help="Search for a quiz without joining")
    parser.add_argument("-q", "--quizName", help="The quiz's name")
    parser.add_argument("-i", "--quizID", help="The quiz's ID")
    parser.add_argument("-m", "--maxCount", type=int, default=20, help="How many quizzes to look for")
    parser.add_argument("-d", "--debug", action="store_true", help="Output go brrrrrrrrrrrr")
    args = parser.parse_args()

    client = QuizClient()
    if args.email and args.password:
        try:
            client.authenticate(args.email, args.password)
            print("AUTHENTICATED")
        except Exception as exc:
            print(f"Login error: {exc}")
            return 1

    if args.search:
        quiz_id = args.quizID
        quiz_name = args.quizName
        if not quiz_id and not quiz_name:
            if client.auth_token and name_or_id():
                quiz_name = input("quizName > ")
            else:
                quiz_id = input("quizID > ")
        try:
            return asyncio.run(run_search(quiz_name, quiz_id, client))
        except (ValueError, RuntimeError) as exc:
            print(f"ERROR: {exc}")
            return 1

    nickname = args.nick or input("name > ")
    pin = args.pin or input("pin > ")
    quiz_id = args.quizID
    quiz_name = args.quizName
    if not quiz_id and not quiz_name:
        if client.auth_token and name_or_id():
            quiz_name = input("quizName > ")
        else:
            quiz_id = input("quizID > ")

    try:
        return asyncio.run(run_game(pin, nickname, quiz_id, quiz_name, client))
    except (ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nBYE!")
        return 0


if __name__ == "__main__":
    sys.exit(main())
