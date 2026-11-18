import asyncio

from starlette.types import Receive, Scope, Send

NO_START_PATH = "/no-start"
NO_COMPLETION_PATH = "/no-completion"
RETURNS_VALUE_PATH = "/returns-value"
HELD_PATH = "/held"
HELD_SOCKET_PATH = "/held-socket"


class RawApp:
    """A bare ASGI app that breaks the contract uvicorn logs about and can hold a request open.

    `entered` is set once a held request runs; it ends when `release` is set.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> object:
        if scope["type"] == "lifespan":
            raise RuntimeError("no lifespan")
        if scope["type"] == "websocket":
            return await self._hold_socket(receive, send)
        path = scope["path"]
        if path == NO_START_PATH:
            return None
        if path == NO_COMPLETION_PATH:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            return None
        if path == HELD_PATH:
            self.entered.set()
            await self.release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
        return "a value" if path == RETURNS_VALUE_PATH else None

    async def _hold_socket(self, receive: Receive, send: Send) -> None:
        await receive()
        await send({"type": "websocket.accept"})
        while (await receive())["type"] != "websocket.disconnect":
            pass
