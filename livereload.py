import asyncio
import json
import threading

from websockets.asyncio.server import serve as ws_serve

from common import Color, log

# Pushes a browser refresh via the LiveReload protocol (the LiveReload
# browser extension speaks this) after a change Manager has already handled —
# an RPC module upgrade, or an .xml/asset change that needed no backend
# action at all because Odoo already serves it live. There's no hook for
# .py-triggered restarts here: those are entirely Odoo's own --dev=reload
# watcher's doing now, invisible to this process, so nothing can be pushed
# for them.


class LiveReload:
    HELLO = json.dumps({
        "command": "hello",
        "protocols": ["http://livereload.com/protocols/official-7"],
        "serverName": "odoo-watch",
    })

    def __init__(self, port: int):
        self.port = port
        self._clients: set = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._reconnecting = False

    def start(self):
        self._thread.start()

    def reload(self, path: str = "/"):
        if not self.is_active():
            return
        msg = json.dumps({"command": "reload", "path": path, "liveCSS": True})
        asyncio.run_coroutine_threadsafe(self._broadcast(msg), self._loop)

    def is_active(self):
        return self._clients and self._loop

    async def _broadcast(self, msg: str):
        for ws in list(self._clients):
            try:
                await ws.send(msg)
            except Exception:
                self._clients.discard(ws)

    async def _handler(self, ws):
        self._clients.add(ws)
        handshake = False
        try:
            await ws.send(self.HELLO)
            async for _ in ws:
                if not handshake:
                    handshake = True
                    if not self._reconnecting:
                        log(f"Live reload client connected ({len(self._clients)} in total)", Color.CYAN)
                    self._reconnecting = False
        finally:
            self._clients.discard(ws)
            if handshake:
                if ws.close_code == 1001:
                    self._reconnecting = True
                else:
                    log(f"Live reload client disconnected ({len(self._clients)} remaining)", Color.DIM)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _serve():
            async with ws_serve(self._handler, "127.0.0.1", self.port):
                await asyncio.Future()  # run forever

        self._loop.run_until_complete(_serve())
