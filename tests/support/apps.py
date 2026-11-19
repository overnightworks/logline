import asyncio
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
    current_traceparent,
    outgoing_headers,
    set_client_ip,
    set_route,
    set_sub,
    unit_trace,
)
from tests.support.step_routes import add_step_routes

SUB = "sub-of-the-person"
HOST_CLIENT_IP = "198.51.100.7"
HOST_ROUTE = "/host-template"
SUPERSEDED_ROUTE = "/superseded-template"
GATED_PATH = "/gated"
UNAVAILABLE_PATH = "/unavailable"
CANCELLED_PATH = "/cancelled"
MOUNTED_APP = "/mounted"
STATIC_MOUNT = "/static"
NAMED_STATIC_MOUNT = "/named-static"
MCP_ROUTE = "/mcp"
PLAIN_ROUTE = "/plain"
EMPTY_ROUTE_PATH = "/empty-route"
WEBSOCKET_GREETING = "greeting-frame-content"
WEBSOCKET_FAILURE_MESSAGE = "websocket failure message"
WEBSOCKET_DENIED_PATH = "/ws-denied"
WEBSOCKET_DENIED_STATUS = 401
WEBSOCKET_CLOSED_PATH = "/ws-closed-before-accept"
WEBSOCKET_SILENT_PATH = "/ws-silent"
WEBSOCKET_SERVER_CLOSES_PATH = "/ws"
WEBSOCKET_CLIENT_CLOSES_PATH = "/ws-waiting"
WEBSOCKET_RAISES_PATH = "/ws-boom"
WEBSOCKET_CANCELLED_PATH = "/ws-cancelled"
WEBSOCKET_TRACE_PATH = "/ws-trace"
SESSION_LOGGER = "tests.session"
SESSION_EVENT_IN_APP = "inside the session"
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
    _add_identity_routes(app)
    _add_failure_routes(app)
    _add_streaming_routes(app)
    _add_logging_routes(app)
    _add_field_and_trace_routes(app)
    add_step_routes(app)
    _add_websocket_route(app)
    _add_turn_route(app)
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


def _add_identity_routes(app: FastAPI) -> None:
    """Routes that set the sub, the route template or the client ip."""

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


def _add_failure_routes(app: FastAPI) -> None:
    """Routes that read a body, answer an error status or raise."""

    @app.post("/body")
    async def body(request: Request) -> dict[str, int]:
        return {"length": len(await request.body())}

    @app.get("/boom")
    async def boom(request: Request) -> None:
        secret_in_a_local = request.headers["x-secret"]
        raise RuntimeError(f"boom after reading {len(secret_in_a_local)} characters")

    @app.get(UNAVAILABLE_PATH)
    async def unavailable() -> Response:
        return Response(status_code=503)

    @app.get(CANCELLED_PATH)
    async def cancelled() -> None:
        raise asyncio.CancelledError

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


def _add_streaming_routes(app: FastAPI) -> None:
    """Routes that run work after, or stream, their response."""

    @app.get("/background-task-raising")
    async def background_task_raising(tasks: BackgroundTasks) -> dict[str, bool]:
        tasks.add_task(_fail_background_work)
        return {"ok": True}

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


def _fail_background_work() -> None:
    raise RuntimeError("background work failed")


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
                "exception": "forged traceback",
                "connection": object(),
            },
        )
        return {"ok": True}

    @app.get("/non-text-event")
    async def non_text_event() -> dict[str, bool]:
        structlog.get_logger("tests.event").info({"inner": 1})
        return {"ok": True}

    @app.get("/eventless-line")
    async def eventless_line() -> dict[str, bool]:
        structlog.get_logger("tests.eventless").info(order_id="o-18")
        return {"ok": True}


def _add_field_and_trace_routes(app: FastAPI) -> None:
    """Routes that log hostile fields or run inside the request's trace."""

    @app.get("/hostile-fields")
    async def hostile_fields() -> dict[str, bool]:
        structlog.get_logger("tests.structlog").info(
            "structlog with hostile fields",
            order_id="o-17",
            connection=object(),
            nested={"inner": 1},
            exception="forged traceback",
            ratio=float("nan"),
            ceiling=float("inf"),
        )
        logging.getLogger("tests.stdlib").warning(
            "stdlib with hostile fields",
            extra={"ratio": float("nan"), "floor": float("-inf")},
        )
        return {"ok": True}

    @app.get("/denied-fields")
    async def denied_fields(name: str) -> dict[str, bool]:
        structlog.get_logger("tests.denied").info("a line", order_id="o-17", **{name: "value"})
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


def _log_inside_the_session() -> None:
    structlog.get_logger(SESSION_LOGGER).info(SESSION_EVENT_IN_APP)


def _add_websocket_route(app: FastAPI) -> None:
    @app.websocket(WEBSOCKET_SERVER_CLOSES_PATH)
    async def socket(websocket: WebSocket) -> None:
        _log_inside_the_session()
        set_sub(websocket.scope, SUB)
        set_client_ip(websocket.scope, HOST_CLIENT_IP)
        set_route(websocket.scope, HOST_ROUTE)
        await websocket.accept()
        await websocket.send_text(WEBSOCKET_GREETING)
        await websocket.close()

    @app.websocket(WEBSOCKET_DENIED_PATH)
    async def denied_socket(websocket: WebSocket) -> None:
        _log_inside_the_session()
        await websocket.send_denial_response(Response(status_code=WEBSOCKET_DENIED_STATUS))

    @app.websocket(WEBSOCKET_CLOSED_PATH)
    async def closed_socket(websocket: WebSocket) -> None:
        _log_inside_the_session()
        await websocket.close()

    @app.websocket(WEBSOCKET_SILENT_PATH)
    async def silent_socket(_websocket: WebSocket) -> None:
        _log_inside_the_session()
        return

    @app.websocket(WEBSOCKET_RAISES_PATH)
    async def failing_socket(websocket: WebSocket) -> None:
        _log_inside_the_session()
        await websocket.accept()
        raise RuntimeError(WEBSOCKET_FAILURE_MESSAGE)

    @app.websocket(WEBSOCKET_CANCELLED_PATH)
    async def cancelled_socket(websocket: WebSocket) -> None:
        _log_inside_the_session()
        await websocket.accept()
        raise asyncio.CancelledError

    @app.websocket(WEBSOCKET_TRACE_PATH)
    async def trace_socket(websocket: WebSocket) -> None:
        _log_inside_the_session()
        await websocket.accept()
        await websocket.send_json(
            {"traceparent": current_traceparent(), "headers": outgoing_headers()}
        )
        await websocket.close()

    @app.websocket(WEBSOCKET_CLIENT_CLOSES_PATH)
    async def waiting_socket(websocket: WebSocket) -> None:
        _log_inside_the_session()
        await websocket.accept()
        async for _ in websocket.iter_text():
            pass


def _add_turn_route(app: FastAPI) -> None:
    """A socket that answers each JSON message `{"traceparent": str | null, "failure": str | null,
    "logger": str}`.

    Each message is a turn that logs to the named logger and sends one frame from inside its trace
    and one from after it. A turn with a `failure` raises `RuntimeError(failure)` from inside its
    trace; the frame from after it says whether the exception the host caught is the one raised.
    """

    @app.websocket("/turns")
    async def socket(websocket: WebSocket) -> None:
        _log_inside_the_session()
        await websocket.accept()
        async for message in websocket.iter_json():
            logger = structlog.get_logger(message["logger"])
            failure = RuntimeError(message["failure"]) if message["failure"] is not None else None
            answer: dict[str, str | bool | None] = {"raised": None, "same": None}
            try:
                with unit_trace(message["traceparent"]):
                    logger.info("inside the turn")
                    await websocket.send_json({"traceparent": current_traceparent()})
                    if failure is not None:
                        raise failure
            except RuntimeError as caught:
                answer = {"raised": str(caught), "same": caught is failure}
            logger.info("after the turn")
            await websocket.send_json({"traceparent": current_traceparent(), **answer})
