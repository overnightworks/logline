import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
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
    StepTimer,
    current_traceparent,
    outgoing_headers,
    set_client_ip,
    set_route,
    set_sub,
    unit_trace,
)

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
BACKGROUND_LOGGER = "tests.background"
BACKGROUND_EVENT = "background work after the response"
CHAIN_CAUSE_MESSAGE = "inner cause message"
CHAIN_MESSAGE = "visible message"
STEP_LOGGER = "tests.steps"
STEP_EVENT = "gateway_call.ended"
STEP_AGENT_ID = "agent-7"
STEP_REFUSAL_REASON = "overloaded"
STEP_FAILURE_MESSAGE = "failure message that stays out of the line"
GROUP_MEMBER_MESSAGES = ("first member message", "second member message")


@dataclass(frozen=True)
class StepScenario:
    """What the step routes add, fail with and collide on, which the step tests read back."""

    failure_reason: str = "rate_limited"
    ok_reason: str = "silence"
    count: int = 42
    later_agent_id: str = "agent-8"
    reserved_field: str = "module"
    earlier_start_seconds: int = 5
    seconds_before_first_output: int = 1
    seconds_after_first_output: int = 2


STEP = StepScenario()


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
    steps = Steps()
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
    _add_step_outcome_routes(app, steps)
    _add_step_reason_routes(app, steps)
    _add_step_lifetime_routes(app, steps)
    _add_step_field_routes(app, steps)
    _add_step_failure_routes(app, steps)
    _add_step_conflict_routes(app, steps)
    _add_step_end_routes(app, steps)
    _add_step_end_cancelled_routes(app, steps)
    _add_step_end_order_routes(app, steps)
    _add_step_start_routes(app, steps)
    _add_step_thread_routes(app, steps)
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


def _add_websocket_route(app: FastAPI) -> None:
    @app.websocket(WEBSOCKET_SERVER_CLOSES_PATH)
    async def socket(websocket: WebSocket) -> None:
        set_sub(websocket.scope, SUB)
        set_client_ip(websocket.scope, HOST_CLIENT_IP)
        set_route(websocket.scope, HOST_ROUTE)
        await websocket.accept()
        await websocket.send_text(WEBSOCKET_GREETING)
        await websocket.close()

    @app.websocket(WEBSOCKET_DENIED_PATH)
    async def denied_socket(websocket: WebSocket) -> None:
        await websocket.send_denial_response(Response(status_code=WEBSOCKET_DENIED_STATUS))

    @app.websocket(WEBSOCKET_CLOSED_PATH)
    async def closed_socket(websocket: WebSocket) -> None:
        await websocket.close()

    @app.websocket(WEBSOCKET_SILENT_PATH)
    async def silent_socket(_websocket: WebSocket) -> None:
        return

    @app.websocket(WEBSOCKET_RAISES_PATH)
    async def failing_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        raise RuntimeError(WEBSOCKET_FAILURE_MESSAGE)

    @app.websocket(WEBSOCKET_CANCELLED_PATH)
    async def cancelled_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        raise asyncio.CancelledError

    @app.websocket(WEBSOCKET_CLIENT_CLOSES_PATH)
    async def waiting_socket(websocket: WebSocket) -> None:
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


class Steps:
    """Builds the steps the routes run, on a clock that moves only when a route advances it."""

    def __init__(self) -> None:
        self._now = 1000.0

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def __call__(self, *, started_at: float | None = None, **fields: str | float) -> StepTimer:
        return _ManualStepTimer(
            self,
            STEP_EVENT,
            logging.getLogger(STEP_LOGGER),
            started_at=started_at,
            **{"agent_id": STEP_AGENT_ID, **fields},
        )


class _ManualStepTimer(StepTimer):
    def __init__(
        self,
        steps: Steps,
        event: str,
        logger: logging.Logger,
        /,
        *,
        started_at: float | None,
        **fields: str | float,
    ) -> None:
        self._steps = steps
        super().__init__(event, logger, started_at=started_at, **fields)

    def _read_clock(self) -> float:
        return self._steps.now()


type _Answer = dict[str, str | bool | None]


def _caught(caught: Exception, raised: Exception) -> _Answer:
    return {"caught": str(caught), "same": caught is raised}


def _sync_step(steps: Steps, *, failure: Exception | None) -> None:
    with steps():
        if failure is not None:
            raise failure


async def _generator_of_a_step(steps: Steps) -> AsyncGenerator[int]:
    async with steps():
        yield 1
        yield 2


def _add_step_outcome_routes(app: FastAPI, steps: Steps) -> None:
    """Routes that run one step to an outcome; the response says which exception got out."""

    @app.get("/step/ok")
    async def step_ok() -> _Answer:
        async with steps():
            pass
        return {"caught": None}

    @app.get("/step/failed")
    async def step_failed() -> _Answer:
        failure = ValueError(STEP_FAILURE_MESSAGE)
        try:
            async with steps():
                raise failure
        except ValueError as caught:
            return _caught(caught, failure)

    @app.get("/step/failed-handled")
    async def step_failed_handled() -> _Answer:
        failure = ValueError(STEP_FAILURE_MESSAGE)
        try:
            async with steps() as step:
                step.handled()
                raise failure
        except ValueError as caught:
            return _caught(caught, failure)

    @app.get("/step/failed-escaping")
    async def step_failed_escaping() -> None:
        async with steps():
            raise ValueError(STEP_FAILURE_MESSAGE)

    @app.get("/step/degraded")
    async def step_degraded() -> _Answer:
        async with steps() as step:
            step.degraded()
        return {"caught": None}

    @app.get("/step/refused")
    async def step_refused() -> _Answer:
        async with steps() as step:
            step.refuse(STEP_REFUSAL_REASON)
        return {"caught": None}

    @app.get("/step/ok-with-reason")
    async def step_ok_with_reason() -> _Answer:
        async with steps() as step:
            step.reason(STEP.ok_reason)
        return {"caught": None}


def _add_step_reason_routes(app: FastAPI, steps: Steps) -> None:
    """Routes whose step gives an ok reason and then ends otherwise."""

    @app.get("/step/reason-then-refuse")
    async def step_reason_then_refuse() -> _Answer:
        async with steps() as step:
            step.reason(STEP.ok_reason)
            step.refuse(STEP_REFUSAL_REASON)
        return {"caught": None}

    @app.get("/step/reason-then-fail")
    async def step_reason_then_fail() -> _Answer:
        async with steps() as step:
            step.reason(STEP.ok_reason)
            step.fail(STEP.failure_reason)
        return {"caught": None}

    @app.get("/step/reason-then-raise")
    async def step_reason_then_raise() -> _Answer:
        failure = ValueError(STEP_FAILURE_MESSAGE)
        try:
            async with steps() as step:
                step.reason(STEP.ok_reason)
                raise failure
        except ValueError as caught:
            return _caught(caught, failure)

    @app.get("/step/reason-then-cancel")
    async def step_reason_then_cancel() -> _Answer:
        step = steps()
        step.reason(STEP.ok_reason)
        step.end_cancelled()
        return {"caught": None}

    @app.get("/step/refused-degraded")
    async def step_refused_degraded() -> _Answer:
        async with steps() as step:
            step.degraded()
            step.refuse(STEP_REFUSAL_REASON)
        return {"caught": None}


def _add_step_lifetime_routes(app: FastAPI, steps: Steps) -> None:
    """Routes whose step is cancelled or lives in a generator."""

    @app.get("/step/cancelled")
    async def step_cancelled() -> _Answer:
        async def wait_forever() -> None:
            async with steps():
                await asyncio.Event().wait()

        waiting = asyncio.create_task(wait_forever())
        await asyncio.sleep(0)
        waiting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiting
        return {"caught": None, "cancelled": waiting.cancelled()}

    @app.get("/step/generator-closed")
    async def step_generator_closed() -> _Answer:
        generator = _generator_of_a_step(steps)
        await anext(generator)
        await generator.aclose()
        return {"caught": None}

    @app.get("/step/generator-finished")
    async def step_generator_finished() -> _Answer:
        async with contextlib.aclosing(_generator_of_a_step(steps)) as generator:
            async for _ in generator:
                pass
        return {"caught": None}


def _add_step_field_routes(app: FastAPI, steps: Steps) -> None:
    """Routes whose step times and adds fields."""

    @app.get("/step/timer-named-fields")
    async def step_timer_named_fields() -> _Answer:
        async with steps(
            duration_ms=-1,
            outcome="failed",
            reason="caller",
            first_output_ms=-1,
            error_type="Caller",
        ):
            pass
        return {"caught": None}

    @app.get("/step/first-output")
    async def step_first_output() -> _Answer:
        async with steps() as step:
            steps.advance(STEP.seconds_before_first_output)
            step.first_output()
            steps.advance(STEP.seconds_after_first_output)
            step.first_output()
        return {"caught": None}

    @app.get("/step/add")
    async def step_add() -> _Answer:
        async with steps() as step:
            step.add(input_tokens=1, output_tokens=STEP.count, duration_ms=-1, reason="caller")
            step.add(input_tokens=STEP.count, agent_id=STEP.later_agent_id, error_type="Caller")
            step.add(started_at=STEP.earlier_start_seconds)
        return {"caught": None}

    @app.get("/step/add-reserved")
    async def step_add_reserved() -> _Answer:
        try:
            async with steps() as step:
                step.add(**{STEP.reserved_field: "value"})
        except ValueError as caught:
            return {"caught": str(caught)}
        return {"caught": None}

    @app.get("/step/add-reserved-escaping")
    async def step_add_reserved_escaping() -> None:
        async with steps() as step:
            step.add(**{STEP.reserved_field: "value"})


def _add_step_failure_routes(app: FastAPI, steps: Steps) -> None:
    """Routes that fail, refuse or degrade a step, with or without a raise."""

    @app.get("/step/refuse-then-raise")
    async def step_refuse_then_raise() -> _Answer:
        failure = ValueError(STEP_FAILURE_MESSAGE)
        try:
            async with steps() as step:
                step.refuse(STEP_REFUSAL_REASON)
                raise failure
        except ValueError as caught:
            return _caught(caught, failure)

    @app.get("/step/fail")
    async def step_fail() -> _Answer:
        async with steps() as step:
            step.fail(STEP.failure_reason)
        return {"caught": None}

    @app.get("/step/degraded-then-fail")
    async def step_degraded_then_fail() -> _Answer:
        async with steps() as step:
            step.degraded()
            step.fail(STEP.failure_reason)
        return {"caught": None}

    @app.get("/step/fail-handled")
    async def step_fail_handled() -> _Answer:
        async with steps() as step:
            step.handled()
            step.fail(STEP.failure_reason)
        return {"caught": None}


def _add_step_conflict_routes(app: FastAPI, steps: Steps) -> None:
    """Routes that give a step two outcomes in turn."""

    @app.get("/step/fail-then-refuse")
    async def step_fail_then_refuse() -> _Answer:
        async with steps() as step:
            step.fail(STEP.failure_reason)
            step.refuse(STEP_REFUSAL_REASON)
        return {"caught": None}

    @app.get("/step/refuse-then-fail")
    async def step_refuse_then_fail() -> _Answer:
        async with steps() as step:
            step.refuse(STEP_REFUSAL_REASON)
            step.fail(STEP.failure_reason)
        return {"caught": None}

    @app.get("/step/fail-then-raise")
    async def step_fail_then_raise() -> _Answer:
        failure = ValueError(STEP_FAILURE_MESSAGE)
        try:
            async with steps() as step:
                step.fail(STEP.failure_reason)
                raise failure
        except ValueError as caught:
            return _caught(caught, failure)

    @app.get("/step/fail-then-cancel")
    async def step_fail_then_cancel() -> _Answer:
        async def wait_forever() -> None:
            async with steps() as step:
                step.fail(STEP.failure_reason)
                await asyncio.Event().wait()

        waiting = asyncio.create_task(wait_forever())
        await asyncio.sleep(0)
        waiting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiting
        return {"caught": None}


def _add_step_end_routes(app: FastAPI, steps: Steps) -> None:
    """Routes that end a step by hand."""

    @app.get("/step/end")
    async def step_end() -> _Answer:
        step = steps()
        step.add(output_tokens=STEP.count)
        step.end()
        return {"caught": None}

    @app.get("/step/fail-then-end")
    async def step_fail_then_end() -> _Answer:
        step = steps()
        step.fail(STEP.failure_reason)
        step.end()
        return {"caught": None}

    @app.get("/step/refuse-then-end")
    async def step_refuse_then_end() -> _Answer:
        step = steps()
        step.refuse(STEP_REFUSAL_REASON)
        step.end()
        return {"caught": None}

    @app.get("/step/end-cancelled")
    async def step_end_cancelled() -> _Answer:
        step = steps()
        step.add(output_tokens=STEP.count)
        step.end_cancelled()
        return {"caught": None}

    @app.get("/step/sync-end-cancelled")
    def step_sync_end_cancelled() -> _Answer:
        steps().end_cancelled()
        return {"caught": None}


def _add_step_end_cancelled_routes(app: FastAPI, steps: Steps) -> None:
    """Routes that end a step as cancelled after another outcome."""

    @app.get("/step/fail-then-end-cancelled")
    async def step_fail_then_end_cancelled() -> _Answer:
        step = steps()
        step.fail(STEP.failure_reason)
        step.end_cancelled()
        return {"caught": None}

    @app.get("/step/refuse-then-end-cancelled")
    async def step_refuse_then_end_cancelled() -> _Answer:
        step = steps()
        step.refuse(STEP_REFUSAL_REASON)
        step.end_cancelled()
        return {"caught": None}

    @app.get("/step/handled-then-end-cancelled")
    async def step_handled_then_end_cancelled() -> _Answer:
        step = steps()
        step.handled()
        step.end_cancelled()
        return {"caught": None}

    @app.get("/step/degraded-then-end-cancelled")
    async def step_degraded_then_end_cancelled() -> _Answer:
        step = steps()
        step.degraded()
        step.end_cancelled()
        return {"caught": None}

    @app.get("/step/end-cancelled-then-raise")
    async def step_end_cancelled_then_raise() -> _Answer:
        failure = ValueError(STEP_FAILURE_MESSAGE)
        try:
            async with steps() as step:
                step.end_cancelled()
                raise failure
        except ValueError as caught:
            return _caught(caught, failure)


def _add_step_end_order_routes(app: FastAPI, steps: Steps) -> None:
    """Routes that end a step more than once or inside its block."""

    @app.get("/step/end-cancelled-then-end")
    async def step_end_cancelled_then_end() -> _Answer:
        step = steps()
        step.end_cancelled()
        step.end()
        return {"caught": None}

    @app.get("/step/end-then-end-cancelled")
    async def step_end_then_end_cancelled() -> _Answer:
        step = steps()
        step.end()
        step.end_cancelled()
        return {"caught": None}

    @app.get("/step/end-cancelled-twice")
    async def step_end_cancelled_twice() -> _Answer:
        step = steps()
        step.end_cancelled()
        step.end_cancelled()
        return {"caught": None}

    @app.get("/step/end-cancelled-in-block")
    async def step_end_cancelled_in_block() -> _Answer:
        async with steps() as step:
            step.end_cancelled()
        return {"caught": None}


def _add_step_start_routes(app: FastAPI, steps: Steps) -> None:
    """Routes that build a step apart from its block, start it earlier or end it synchronously."""

    @app.get("/step/sync-end")
    def step_sync_end() -> _Answer:
        steps().end()
        return {"caught": None}

    @app.get("/step/constructed-before-the-block")
    async def step_constructed_before_the_block() -> _Answer:
        step = steps()
        steps.advance(STEP.seconds_before_first_output)
        step.first_output()
        steps.advance(STEP.seconds_after_first_output)
        async with step:
            pass
        return {"caught": None}

    @app.get("/step/started-earlier")
    async def step_started_earlier() -> _Answer:
        step = steps(started_at=steps.now() - STEP.earlier_start_seconds)
        step.first_output()
        steps.advance(STEP.seconds_after_first_output)
        step.end()
        return {"caught": None}

    @app.get("/step/started-in-the-future")
    async def step_started_in_the_future() -> _Answer:
        try:
            steps(started_at=steps.now() + STEP.earlier_start_seconds)
        except ValueError as caught:
            return {"caught": str(caught)}
        return {"caught": None}

    @app.get("/step/end-twice")
    async def step_end_twice() -> _Answer:
        step = steps()
        step.end()
        step.end()
        return {"caught": None}

    @app.get("/step/end-in-block")
    async def step_end_in_block() -> _Answer:
        async with steps() as step:
            step.end()
        return {"caught": None}

    @app.get("/step/end-then-raise")
    async def step_end_then_raise() -> _Answer:
        failure = ValueError(STEP_FAILURE_MESSAGE)
        try:
            async with steps() as step:
                step.end()
                raise failure
        except ValueError as caught:
            return _caught(caught, failure)


def _add_step_thread_routes(app: FastAPI, steps: Steps) -> None:
    """Routes that run a step on a thread."""

    @app.get("/step/thread")
    async def step_thread() -> _Answer:
        await asyncio.to_thread(_sync_step, steps, failure=None)
        return {"caught": None}

    @app.get("/step/thread-failed")
    async def step_thread_failed() -> _Answer:
        failure = ValueError(STEP_FAILURE_MESSAGE)
        try:
            await asyncio.to_thread(_sync_step, steps, failure=failure)
        except ValueError as caught:
            return _caught(caught, failure)
        return {"caught": None}

    @app.get("/step/sync-endpoint")
    def step_sync_endpoint() -> _Answer:
        _sync_step(steps, failure=None)
        return {"caught": None}
