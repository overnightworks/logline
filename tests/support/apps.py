import logging
from collections.abc import AsyncIterator
from pathlib import Path

import structlog
from fastapi import BackgroundTasks, FastAPI, Request, WebSocket
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from logline import (
    RequestLineMiddleware,
    outgoing_headers,
    set_client_ip,
    set_route,
    set_sub,
)

SUB = "sub-of-the-person"
HOST_CLIENT_IP = "198.51.100.7"
HOST_ROUTE = "/host-template"
SUPERSEDED_ROUTE = "/superseded-template"
GATED_PATH = "/gated"
MOUNTED_APP = "/mounted"
STATIC_MOUNT = "/static"
NAMED_STATIC_MOUNT = "/named-static"
MCP_ROUTE = "/mcp"
PLAIN_ROUTE = "/plain"
EMPTY_ROUTE_PATH = "/empty-route"
WEBSOCKET_GREETING = "greeting-frame-content"
BACKGROUND_LOGGER = "tests.background"
BACKGROUND_EVENT = "background work after the response"
CHAIN_CAUSE_MESSAGE = "inner cause message"
CHAIN_MESSAGE = "visible message"
GROUP_MEMBER_MESSAGES = ("first member message", "second member message")


async def _pass_through(request: Request, call_next: RequestResponseEndpoint) -> Response:
    return await call_next(request)


class _AnswersUnauthorizedAtGatedPath:
    """A pure-ASGI gate in front of the router, as a host's sign-in gate sits."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == GATED_PATH:
            await Response(status_code=401)(scope, receive, send)
            return
        await self._app(scope, receive, send)


class _NamedRoute:
    """An ASGI app below a `Mount` that names its route for the request line."""

    def __init__(self, template: str, app: ASGIApp) -> None:
        self._template = template
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        set_route(scope, self._template)
        await self._app(scope, receive, send)


def build_app(
    *, below_base_http_middleware: bool = False, static_directory: Path | None = None
) -> FastAPI:
    """A FastAPI app behind logline's middleware, one route for each behaviour a test drives.

    With a `static_directory` it also mounts a FastAPI app and that directory twice, once of them
    naming its route.
    """
    app = FastAPI()
    if below_base_http_middleware:
        app.add_middleware(BaseHTTPMiddleware, dispatch=_pass_through)
    app.add_middleware(_AnswersUnauthorizedAtGatedPath)
    app.add_middleware(RequestLineMiddleware)
    if static_directory is not None:
        _add_mounts(app, static_directory)
    _add_request_routes(app)
    _add_logging_routes(app)
    _add_websocket_route(app)
    return app


def _add_mounts(app: FastAPI, static_directory: Path) -> None:
    mounted_app = FastAPI()

    @mounted_app.get("/{name}")
    async def inner_route(name: str) -> dict[str, str]:
        return {"name": name}

    app.mount(MOUNTED_APP, mounted_app)
    app.mount(STATIC_MOUNT, StaticFiles(directory=static_directory))
    app.mount(
        NAMED_STATIC_MOUNT,
        _NamedRoute(NAMED_STATIC_MOUNT, StaticFiles(directory=static_directory)),
    )


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

    @app.get("/async-route")
    async def async_route(request: Request) -> dict[str, bool]:
        set_route(request.scope, HOST_ROUTE)
        return {"ok": True}

    @app.get("/reset-route")
    async def reset_route(request: Request) -> dict[str, bool]:
        set_route(request.scope, SUPERSEDED_ROUTE)
        set_route(request.scope, HOST_ROUTE)
        return {"ok": True}

    @app.get(EMPTY_ROUTE_PATH)
    async def empty_route(request: Request) -> dict[str, bool]:
        set_route(request.scope, "")
        return {"ok": True}

    @app.get("/sync-route")
    def sync_route(request: Request) -> dict[str, bool]:
        set_route(request.scope, HOST_ROUTE)
        return {"ok": True}

    async def mcp_front(request: Request) -> JSONResponse:
        set_route(request.scope, MCP_ROUTE)
        return JSONResponse({"ok": True})

    async def plain_front(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app.router.routes.append(Route(MCP_ROUTE, mcp_front))
    app.router.routes.append(Route(PLAIN_ROUTE, plain_front))

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

    @app.get("/library-line-with-fields")
    async def library_line_with_fields() -> dict[str, bool]:
        logging.getLogger("some_library").warning(
            "library with fields",
            extra={
                "room_id": "r-1",
                "duration_ms": 12,
                "trace_id": "forged",
                "service": "forged",
                "event": "forged",
                "connection": object(),
            },
        )
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
        set_route(websocket.scope, HOST_ROUTE)
        await websocket.accept()
        await websocket.send_text(WEBSOCKET_GREETING)
        await websocket.close()
