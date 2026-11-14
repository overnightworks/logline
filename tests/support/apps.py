import logging
from collections.abc import AsyncIterator
from pathlib import Path

import structlog
from fastapi import BackgroundTasks, FastAPI, Request, WebSocket
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response, StreamingResponse
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from logline import RequestLineMiddleware, outgoing_headers, set_client_ip, set_sub

SUB = "sub-of-the-person"
HOST_CLIENT_IP = "198.51.100.7"
WEBSOCKET_GREETING = "greeting-frame-content"
BACKGROUND_LOGGER = "tests.background"
BACKGROUND_EVENT = "background work after the response"
CHAIN_CAUSE_MESSAGE = "inner cause message"
CHAIN_MESSAGE = "visible message"
GROUP_MEMBER_MESSAGES = ("first member message", "second member message")


async def _pass_through(request: Request, call_next: RequestResponseEndpoint) -> Response:
    return await call_next(request)


def build_app(*, below_base_http_middleware: bool = False) -> FastAPI:
    """A FastAPI app behind logline's middleware, one route for each behaviour a test drives."""
    app = FastAPI()
    if below_base_http_middleware:
        app.add_middleware(BaseHTTPMiddleware, dispatch=_pass_through)
    app.add_middleware(RequestLineMiddleware)
    _add_request_routes(app)
    _add_logging_routes(app)
    _add_websocket_route(app)
    return app


def _add_request_routes(app: FastAPI) -> None:
    @app.get("/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    @app.get("/async-sub")
    async def async_sub(request: Request) -> dict[str, bool]:
        set_sub(request.scope, SUB)
        return {"ok": True}

    @app.get("/sync-sub")
    def sync_sub(request: Request) -> dict[str, bool]:
        set_sub(request.scope, SUB)
        return {"ok": True}

    @app.get("/host-client-ip")
    async def host_client_ip(request: Request) -> dict[str, bool]:
        set_client_ip(request.scope, HOST_CLIENT_IP)
        return {"ok": True}

    @app.post("/body")
    async def body(request: Request) -> dict[str, int]:
        return {"length": len(await request.body())}

    @app.get("/boom")
    async def boom(request: Request) -> None:
        secret_in_a_local = request.headers["x-secret"]
        raise RuntimeError(f"boom after reading {len(secret_in_a_local)} characters")

    @app.get("/chained")
    async def chained() -> None:
        _raise_chained()

    @app.get("/chained-sync")
    def chained_sync() -> None:
        _raise_chained()

    @app.get("/group")
    async def group() -> None:
        first, second = GROUP_MEMBER_MESSAGES
        raise ExceptionGroup("group message", [ValueError(first), TypeError(second)])

    @app.get("/background-task")
    async def background_task(tasks: BackgroundTasks) -> dict[str, bool]:
        tasks.add_task(_log_background_work)
        return {"ok": True}

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def chunks() -> AsyncIterator[bytes]:
            yield b"first chunk"
            yield b"last chunk"

        return StreamingResponse(chunks())


def _raise_chained() -> None:
    try:
        raise KeyError(CHAIN_CAUSE_MESSAGE)
    except KeyError as cause:
        raise ValueError(CHAIN_MESSAGE) from cause


def _log_background_work() -> None:
    structlog.get_logger(BACKGROUND_LOGGER).info(BACKGROUND_EVENT)


def _add_logging_routes(app: FastAPI) -> None:
    @app.get("/library-line")
    async def library_line() -> dict[str, bool]:
        logging.getLogger("some_library").warning("library says %s", "hello")
        return {"ok": True}

    @app.get("/trace")
    async def trace() -> dict[str, dict[str, str]]:
        structlog.get_logger("tests.app").info("inside the request")
        return {"headers": outgoing_headers()}

    @app.get("/trace-sync")
    def trace_sync() -> dict[str, dict[str, str]]:
        structlog.get_logger("tests.app").info("inside the request, on a worker thread")
        logging.getLogger("some_library").warning("library line on a worker thread")
        return {"headers": outgoing_headers()}


def _add_websocket_route(app: FastAPI) -> None:
    @app.websocket("/ws")
    async def socket(websocket: WebSocket) -> None:
        set_sub(websocket.scope, SUB)
        set_client_ip(websocket.scope, HOST_CLIENT_IP)
        await websocket.accept()
        await websocket.send_text(WEBSOCKET_GREETING)
        await websocket.close()


STATIC_MOUNT = "/static"
SUB_APP_MOUNT = "/mcp"
SUB_APP_ROUTE = "/{x}"
GATED_PATH = "/gated"
STATIC_FILE_NAME = "readme-of-the-static-files.txt"


class _DenyingGate:
    """A pure-ASGI gate in front of the router: it answers GATED_PATH with 401 and nothing else."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == GATED_PATH:
            await Response(status_code=401)(scope, receive, send)
            return
        await self._app(scope, receive, send)


def build_mounted_app(static_directory: Path) -> FastAPI:
    """A FastAPI app: a static-files mount, a mounted sub-app and a gate before the router."""
    app = FastAPI()
    sub_app = FastAPI()

    @sub_app.get(SUB_APP_ROUTE)
    async def sub_route(x: str) -> dict[str, str]:
        return {"x": x}

    app.mount(STATIC_MOUNT, StaticFiles(directory=static_directory))
    app.mount(SUB_APP_MOUNT, sub_app)
    app.add_middleware(_DenyingGate)
    app.add_middleware(RequestLineMiddleware)
    return app
