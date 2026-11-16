#!/usr/bin/env python3
"""Patch the vendored aiocometd 0.4.5 so it works on Python 3.10+.

Upstream aiocometd 0.4.5 (2019) passes the removed ``loop=`` keyword to
asyncio primitives and removed the ``loop`` argument of ``asyncio.sleep``,
``asyncio.wait``, ``asyncio.wait_for`` and ``asyncio.ensure_future``.

Python 3.10 removed those parameters, so the library blows up with
``TypeError: Semaphore.__init__() got an unexpected keyword argument 'loop'``
as soon as it is used on a modern interpreter.

Run this script from the repository root:

    python vendor/patch_aiocometd.py

It is idempotent: already patched files are detected and left alone.
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent / "aiocometd" / "aiocometd"

PATCHES: list[tuple[str, str, str]] = [
    # ---------------------------------------------------------------- utils
    (
        "utils.py",
        "            await asyncio.sleep(delay, loop=loop)  # type: ignore",
        "            await asyncio.sleep(delay)",
    ),
    # ------------------------------------------------------------ transports
    (
        "transports/long_polling.py",
        "        self._http_semaphore = asyncio.Semaphore(2, loop=self._loop)",
        "        self._http_semaphore = asyncio.Semaphore(2)",
    ),
    (
        "transports/websocket.py",
        '        future: "asyncio.Future[JsonObject]" = asyncio.Future(loop=self._loop)',
        '        future: "asyncio.Future[JsonObject]" = self._loop.create_future()',
    ),
    (
        "transports/base.py",
        "        self._connect_task = asyncio.ensure_future(coro, loop=self._loop)",
        "        self._connect_task = asyncio.ensure_future(coro)",
    ),
    # --------------------------------------------------------------- client
    (
        "client.py",
        """            timeout_task = asyncio.ensure_future(
                self._wait_connection_timeout(connection_timeout),
                loop=self._loop
            )""",
        """            timeout_task = asyncio.ensure_future(
                self._wait_connection_timeout(connection_timeout)
            )""",
    ),
    (
        "client.py",
        """        get_task = asyncio.ensure_future(self._incoming_queue.get(),
                                         loop=self._loop)""",
        """        get_task = asyncio.ensure_future(self._incoming_queue.get())""",
    ),
    (
        "client.py",
        """        server_disconnected_task = asyncio.ensure_future(
            self._transport.wait_for_state(
                TransportState.SERVER_DISCONNECTED),
            loop=self._loop
        )""",
        """        server_disconnected_task = asyncio.ensure_future(
            self._transport.wait_for_state(
                TransportState.SERVER_DISCONNECTED)
        )""",
    ),
    (
        "client.py",
        """            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
                loop=self._loop)""",
        """            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED)""",
    ),
    (
        "client.py",
        """                await asyncio.wait_for(
                    self._transport.wait_for_state(TransportState.CONNECTED),
                    timeout, loop=self._loop
                )""",
        """                await asyncio.wait_for(
                    self._transport.wait_for_state(TransportState.CONNECTED),
                    timeout
                )""",
    ),
    # ------------------------------------------------------------ constants
    # Default to a direct websocket handshake. The long-polling handshake
    # costs an extra HTTP round trip before the game socket is usable, which
    # eats into the answer latency we care about.
    (
        "constants.py",
        "#: Connection type that all CometD server implementaions should support\nDEFAULT_CONNECTION_TYPE = ConnectionType.LONG_POLLING",
        "#: Connection type that all CometD server implementaions should support\n"
        "#: (overridable with KAHOOT_COMETD_TRANSPORT=long-polling)\n"
        "import os\n\n"
        'DEFAULT_CONNECTION_TYPE = ConnectionType(\n'
        '    os.environ.get("KAHOOT_COMETD_TRANSPORT", "websocket"))',
    ),
    # ------------------------------------------------------------ transports
    # The modern Kahoot client attaches a timetrack to every outgoing
    # message. The server uses it for latency accounting and for bot
    # validation, so messages without it can be treated as invalid traffic.
    (
        "transports/base.py",
        """        if "connectionType" in message:
            message["connectionType"] = self.connection_type.value""",
        """        if "connectionType" in message:
            message["connectionType"] = self.connection_type.value

        # Kahoot expects a timetrack extension on every message
        if "ext" not in message:
            message["ext"] = {"timetrack": int(time.time() * 1000)}""",
    ),
    (
        "transports/base.py",
        "import asyncio\n",
        "import asyncio\nimport time\n",
    ),
]

LOOP_RESOLUTION = '''

def _resolve_loop(loop: Optional[asyncio.AbstractEventLoop] = None) -> asyncio.AbstractEventLoop:
    """Return a usable event loop on Python 3.10+.

    ``asyncio.get_event_loop()`` no longer creates a loop implicitly on newer
    interpreters (it raises ``RuntimeError`` from 3.14 on), so fall back to the
    running loop, then to the policy's loop, then to a freshly created one.
    """
    if loop is not None:
        return loop
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        pass
    try:
        return asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        return new_loop
'''

LOOP_RESOLUTION_PATCHES: list[tuple[str, str, str]] = [
    (
        "client.py",
        "        self._loop = loop or asyncio.get_event_loop()",
        "        self._loop = _resolve_loop(loop)",
    ),
    (
        "transports/base.py",
        "        self._loop = loop or asyncio.get_event_loop()",
        "        self._loop = _resolve_loop(loop)",
    ),
]


def _patch_file(path: pathlib.Path, old: str, new: str) -> bool:
    text = path.read_text()
    if old not in text:
        return False
    path.write_text(text.replace(old, new))
    return True


def main() -> int:
    if not ROOT.is_dir():
        print(f"vendored package not found at {ROOT}", file=sys.stderr)
        return 1

    changed = 0
    for rel, old, new in PATCHES + LOOP_RESOLUTION_PATCHES:
        path = ROOT / rel
        if _patch_file(path, old, new):
            changed += 1

    # inject the loop resolver helper into the two modules that need it
    for rel in ("client.py", "transports/base.py"):
        path = ROOT / rel
        text = path.read_text()
        if "def _resolve_loop(" in text:
            continue
        lines = text.splitlines(keepends=True)
        # insert the helper right after the last top level import statement
        tree = ast.parse(text)
        insert_at = 0
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                insert_at = node.end_lineno
        lines.insert(insert_at, LOOP_RESOLUTION)
        path.write_text("".join(lines))
        changed += 1

    print(f"applied {changed} patch operation(s) to {ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
