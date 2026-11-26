import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, WebSocket

from logline import StepTimer, current_traceparent, outgoing_headers, unit_trace
from tests.support.step_routes import STEP_EVENT, STEP_LOGGER

CALLEE_PATH = "/span/callee"
TURNS_PATH = "/span/turns"


def _step(position: str | None = None) -> StepTimer:
    return StepTimer(STEP_EVENT, logging.getLogger(STEP_LOGGER), position=position)


async def _generator_of_a_step() -> AsyncGenerator[int]:
    async with _step():
        yield 1
        yield 2


@asynccontextmanager
async def _step_as_a_context_manager() -> AsyncIterator[None]:
    async with _step():
        yield


def _add_nesting_routes(app: FastAPI) -> None:
    """Routes whose steps run inside other steps, tasks or an outgoing call."""

    @app.get("/span/nested")
    async def nested() -> dict[str, str | None]:
        async with _step("outer"):
            async with _step("inner"):
                return {"traceparent": current_traceparent()}

    @app.get("/span/call")
    async def call(request: Request) -> dict[str, str | None]:
        async with _step(), httpx.AsyncClient() as client:
            await client.get(
                f"{request.base_url}{CALLEE_PATH.lstrip('/')}", headers=outgoing_headers()
            )
        return {}

    @app.get(CALLEE_PATH)
    async def callee() -> dict[str, str | None]:
        return {}

    @app.get("/span/task")
    async def task() -> dict[str, str | None]:
        async def run() -> None:
            async with _step():
                pass

        await asyncio.create_task(run())
        return {}

    @app.get("/span/after/{how}")
    async def after(how: str) -> dict[str, str | None]:
        """Two steps one after the other; the first one ends by `how`."""
        async with _step("first") as first:
            if how == "end":
                first.end()
            if how == "end-cancelled":
                first.end_cancelled()
            if how == "end-failed":
                first.end_failed(ValueError())
        async with _step("second"):
            pass
        return {}

    @app.get("/span/entered-after/{how}")
    async def entered_after(how: str) -> dict[str, str | None]:
        """A step whose line `how` wrote, then entered as a block with a step inside."""
        step = _step("written")
        if how == "end":
            step.end()
        elif how == "end-failed":
            step.end_failed(ValueError())
        else:
            step.end_cancelled()
        async with step:
            async with _step("inside"):
                pass
        return {}


def _add_generator_routes(app: FastAPI) -> None:
    """Routes whose step lives in a generator or a context manager."""

    @app.get("/span/generator-dropped")
    async def generator_dropped() -> dict[str, str | None]:
        generator = _generator_of_a_step()
        await anext(generator)
        del generator
        await asyncio.sleep(0)
        return {}

    @app.get("/span/context-manager")
    async def context_manager() -> dict[str, str | None]:
        async with _step_as_a_context_manager():
            pass
        return {}


def _add_turn_route(app: FastAPI) -> None:
    """A socket that answers each JSON message `{"traceparent": str | null, "do": str}`.

    A message is a turn. `step` runs a step in it and answers the traceparent of the step's span;
    `enter` enters a step the socket keeps, `exit` ends it in a later turn, and `exit-then-step`
    ends it and then runs a step in that same turn.
    """

    @app.websocket(TURNS_PATH)
    async def socket(websocket: WebSocket) -> None:
        await websocket.accept()
        kept: StepTimer | None = None
        async for message in websocket.iter_json():
            answer: dict[str, str | None] = {"traceparent": None}
            with unit_trace(message["traceparent"]):
                match message["do"]:
                    case "step":
                        async with _step():
                            answer = {"traceparent": current_traceparent()}
                    case "enter":
                        kept = _step()
                        await kept.__aenter__()
                    case "exit":
                        assert kept is not None
                        await kept.__aexit__(None, None, None)
                    case _:
                        assert kept is not None
                        await kept.__aexit__(None, None, None)
                        async with _step():
                            answer = {"traceparent": current_traceparent()}
            await websocket.send_json(answer)


def add_span_routes(app: FastAPI) -> None:
    """Add every route whose steps the span tests read."""
    _add_nesting_routes(app)
    _add_generator_routes(app)
    _add_turn_route(app)
