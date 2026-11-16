#!/usr/bin/env python3
"""Dev tool: join a game and dump every raw protocol message.

    python tools/capture.py <pin> [nickname]

Useful to see what Kahoot actually sends for a live game (question text,
choice order, answer maps, quiz ids).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

sys.path.insert(0, ".")

from kahoot_core.challenge import solve_challenge, xor_session_token  # noqa: E402
from kahoot_core.quiz import QuizClient  # noqa: E402

import aiocometd  # noqa: E402

MESSAGE_NAMES = {
    1: "GET_READY", 2: "START_QUESTION", 3: "GAME_OVER", 4: "TIME_UP",
    5: "PLAY_AGAIN", 6: "ANSWER_SELECTED", 7: "ANSWER_RESPONSE",
    8: "REVEAL_ANSWER", 9: "START_QUIZ", 10: "RESET_CONTROLLER",
    13: "REVEAL_RANKING", 14: "USERNAME_ACCEPTED", 15: "USERNAME_REJECTED",
    50: "SUBMIT_TWO_FACTOR", 51: "TWO_FACTOR_AUTH_INCORRECT",
    52: "TWO_FACTOR_AUTH_CORRECT", 53: "RESET_TWO_FACTOR_AUTH",
}


async def main(pin: str, nickname: str, use_captcha: bool = True) -> None:
    client = QuizClient()
    url = f"https://kahoot.it/reserve/session/{pin}/?{int(time.time())}"
    response = await asyncio.to_thread(client.session.get, url)
    print("reserve status:", response.status_code)
    print("reserve body:", response.text[:400])
    token = response.headers.get("x-kahoot-session-token")
    challenge = response.json()["challenge"]
    solution = await asyncio.to_thread(solve_challenge, challenge)
    session_id = xor_session_token(token, solution)
    print("session id:", session_id)

    login = {
        "host": "kahoot.it",
        "gameid": pin,
        "name": nickname,
        "type": "login",
    }
    if use_captcha:
        login["captchaToken"] = "KAHOOT_TOKEN_eyJ2ZXJzaW9uIjoiIn0="

    ws_url = f"wss://play.kahoot.it/cometd/{pin}/{session_id}"
    async with aiocometd.Client(ws_url, ssl=True) as socket:
        await socket.subscribe("/service/controller")
        await socket.subscribe("/service/player")
        await socket.subscribe("/service/status")
        await socket.publish("/service/controller", login)
        print("--- connected, dumping messages ---")
        async for raw in socket:
            message = raw.get("data", {})
            stamp = time.strftime("%H:%M:%S")
            channel = raw.get("channel")
            if "id" in message:
                name = MESSAGE_NAMES.get(message["id"], str(message["id"]))
                print(f"[{stamp}] channel={channel} id={message['id']} ({name})")
                try:
                    content = json.loads(message.get("content", "{}"))
                    print(json.dumps(content, indent=2, ensure_ascii=False)[:4000])
                except Exception:
                    print("   raw:", str(message.get("content"))[:600])
            else:
                print(f"[{stamp}] channel={channel} {json.dumps(message)[:900]}")


if __name__ == "__main__":
    pin = sys.argv[1]
    nickname = sys.argv[2] if len(sys.argv) > 2 else "Capturer"
    use_captcha = "--no-captcha" not in sys.argv
    asyncio.run(main(pin, nickname, use_captcha))
