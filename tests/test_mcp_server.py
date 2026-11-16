"""End to end test of the MCP server over stdio.

It starts the real server as a subprocess, talks to it with the MCP client and
checks that the tools are exposed and that a bogus pin is reported as a clean
error instead of crashing the server.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = pathlib.Path(__file__).resolve().parent.parent
PYTHON = sys.executable


@pytest.mark.anyio
async def test_tools_are_exposed_and_bad_pin_is_handled():
    params = StdioServerParameters(
        command=PYTHON,
        args=["-m", "kahoot_mcp"],
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {
                "join_game",
                "game_status",
                "get_answers",
                "submit_2fa",
                "stop_game",
                "find_quiz",
                "quiz_details",
                "list_sessions",
            } <= names

            result = await session.call_tool("list_sessions", {})
            assert result.is_error is False

            result = await session.call_tool(
                "join_game", {"pin": "000000", "nickname": "tester"}
            )
            assert result.is_error is False

            status = await session.call_tool("game_status", {})
            assert status.is_error is False
            payload = (status.structured_content or {}).get("result", {})
            assert payload["state"] in ("error", "connecting", "in_lobby", "joined")
            assert payload["error"], "a bogus pin must be reported as an error"

            await session.call_tool("stop_game", {})


@pytest.fixture
def anyio_backend():
    return "asyncio"
