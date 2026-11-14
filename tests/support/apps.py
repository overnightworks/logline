import logging
from collections.abc import AsyncIterator

import httpx
import structlog
from fastapi import BackgroundTasks, FastAPI, Request, WebSocket
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import RedirectResponse, Response, StreamingResponse

from logline import RequestLineMiddleware, outgoing_headers, set_client_ip, set_sub

SUB = "sub-of-the-person"
HOST_CLIENT_IP = "198.51.100.7"
WEBSOCKET_GREETING = "greeting-frame-content"
BACKGROUND_LOGGER = "tests.background"
BACKGROUND_EVENT = "background work after the response"
UPSTREAM_QUERY_SECRET = "upstream-query-secret"
UPSTREAM_LOCATION_SECRET = "upstream-location-secret"


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

    @app.get("/redirecting-upstream")
    async def redirecting_upstream() -> RedirectResponse:
        return RedirectResponse(f"/elsewhere?token={UPSTREAM_LOCATION_SECRET}")

    @app.get("/failing-upstream-call")
    async def failing_upstream_call(request: Request) -> None:
        async with httpx.AsyncClient(base_url=str(request.base_url)) as client:
            response = await client.get(f"/redirecting-upstream?key={UPSTREAM_QUERY_SECRET}")
        response.raise_for_status()

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
