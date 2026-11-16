# Vendored aiocometd

This is `aiocometd` 0.4.5 (MIT, by Róbert Márki) with two changes:

1. **Python 3.10+ compatibility.** Upstream passes the removed `loop=`
   keyword to asyncio primitives (`asyncio.Semaphore`, `asyncio.Future`,
   `asyncio.sleep`, `asyncio.wait`, `asyncio.wait_for`,
   `asyncio.ensure_future`). On Python 3.10+ that raises
   `TypeError: Semaphore.__init__() got an unexpected keyword argument 'loop'`.
2. **Websocket-first handshake.** The default CometD transport is switched
   from `long-polling` to `websocket` (Kahoot supports both). The long-polling
   handshake costs an extra HTTP round trip before the game socket is usable,
   which shows up as answer latency.

Set `KAHOOT_COMETD_TRANSPORT=long-polling` to restore the upstream default.

The patches are applied by `vendor/patch_aiocometd.py` and are idempotent:

```sh
python vendor/patch_aiocometd.py
```

Upstream: <https://github.com/robertmrk/aiocometd>
