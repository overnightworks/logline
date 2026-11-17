import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Literal

import pytest
import uvicorn
from fastapi import FastAPI
from starlette.types import Receive, Scope, Send

from logline import RequestLineMiddleware, configure
from logline.asgi import Message
from logline.lifespan import LIFESPAN_LOGGER_NAME
from logline.standard import BASE_KEYS
from tests.support.apps import build_app
from tests.support.lines import Line, json_lines, lines_of
from tests.support.serving import serve

_FAILURE_MESSAGE = "lifespan failure message"
_UVICORN_LOGGER = "uvicorn.error"

type App = Callable[[Scope, Receive, Send], Awaitable[None]]


def _starlette_app(*, fail_at_startup: bool) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if fail_at_startup:
            raise RuntimeError(_FAILURE_MESSAGE)
        yield
        raise RuntimeError(_FAILURE_MESSAGE)

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(RequestLineMiddleware)
    return app


async def _reports_startup_failure_outside_an_exception(
    scope: Scope, receive: Receive, send: Send
) -> None:
    await receive()
    await send({"type": "lifespan.startup.failed", "message": _FAILURE_MESSAGE})


async def _raises_after_receiving_startup(scope: Scope, receive: Receive, send: Send) -> None:
    await receive()
    raise RuntimeError(_FAILURE_MESSAGE)


async def _raises_after_receiving_shutdown(scope: Scope, receive: Receive, send: Send) -> None:
    await receive()
    await send({"type": "lifespan.startup.complete"})
    await receive()
    raise RuntimeError(_FAILURE_MESSAGE)


async def _answers_the_lifespan(scope: Scope, receive: Receive, send: Send) -> None:
    await receive()
    await send({"type": "lifespan.startup.complete"})
    await receive()
    await send({"type": "lifespan.shutdown.complete"})


async def _raises_before_receiving_startup(scope: Scope, receive: Receive, send: Send) -> None:
    raise RuntimeError(_FAILURE_MESSAGE)


def _behind_the_middleware(app: App) -> RequestLineMiddleware:
    return RequestLineMiddleware(app)


def _serve_lifespan(app: App, lifespan: Literal["on", "auto"]) -> None:
    async def nothing(_base_url: str) -> None:
        return None

    # A failed startup exits the process in recent uvicorn; older releases return from `serve`.
    with contextlib.suppress(SystemExit):
        serve(uvicorn.Config(app, log_config=None, lifespan=lifespan), nothing)


def _events(lines: list[Line], logger: str) -> list[object]:
    return [line["event"] for line in lines_of(lines, logger)]


@pytest.mark.parametrize(
    ("app", "events"),
    [
        pytest.param(
            _starlette_app(fail_at_startup=True),
            ["app_startup.failed"],
            id="startup failed by message",
        ),
        pytest.param(
            _starlette_app(fail_at_startup=False),
            ["app_startup.completed", "app_shutdown.failed"],
            id="shutdown failed by message",
        ),
        pytest.param(
            _behind_the_middleware(_raises_after_receiving_startup),
            ["app_startup.failed"],
            id="raised after startup was received",
        ),
        pytest.param(
            _behind_the_middleware(_raises_after_receiving_shutdown),
            ["app_startup.completed", "app_shutdown.failed"],
            id="raised after shutdown was received",
        ),
    ],
)
def test_a_failed_lifespan_phase_is_one_error_line_with_its_traceback(
    capsys: pytest.CaptureFixture[str], app: App, events: list[str]
) -> None:
    configure("lifespan-service")

    _serve_lifespan(app, "on")

    lines = json_lines(capsys.readouterr().out)
    assert _events(lines, LIFESPAN_LOGGER_NAME) == events
    failed = lines_of(lines, LIFESPAN_LOGGER_NAME)[-1]
    assert failed["level"] == "error"
    assert failed["error_type"] == "RuntimeError"
    assert str(failed["exception"]).startswith("Traceback (most recent call last):")
    assert failed.keys() <= {*BASE_KEYS, "error_type", "exception"}
    assert _FAILURE_MESSAGE not in json.dumps(lines_of(lines, _UVICORN_LOGGER))


def test_a_failure_reported_outside_an_exception_has_neither_error_type_nor_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("lifespan-service")

    _serve_lifespan(_behind_the_middleware(_reports_startup_failure_outside_an_exception), "on")

    lines = json_lines(capsys.readouterr().out)
    (failed,) = lines_of(lines, LIFESPAN_LOGGER_NAME)
    assert failed["event"] == "app_startup.failed"
    assert failed.keys() == BASE_KEYS
    assert _FAILURE_MESSAGE not in json.dumps(lines_of(lines, _UVICORN_LOGGER))


def test_an_exception_before_startup_was_received_writes_no_lifespan_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("lifespan-service")

    _serve_lifespan(_behind_the_middleware(_raises_before_receiving_startup), "auto")

    assert lines_of(json_lines(capsys.readouterr().out), LIFESPAN_LOGGER_NAME) == []


def test_the_completed_lines_follow_the_root_level(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "warning")
    configure("lifespan-service")

    _serve_lifespan(build_app(), "on")

    assert lines_of(json_lines(capsys.readouterr().out), LIFESPAN_LOGGER_NAME) == []


def test_a_completed_lifespan_is_one_info_line_for_each_phase(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("lifespan-service")

    _serve_lifespan(build_app(), "on")

    lines = lines_of(json_lines(capsys.readouterr().out), LIFESPAN_LOGGER_NAME)
    assert [(line["level"], line["event"]) for line in lines] == [
        ("info", "app_startup.completed"),
        ("info", "app_shutdown.completed"),
    ]
    assert all(line.keys() == BASE_KEYS for line in lines)


def test_an_app_without_the_middleware_writes_no_lifespan_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("lifespan-service")

    _serve_lifespan(_answers_the_lifespan, "on")

    lines = json_lines(capsys.readouterr().out)
    assert not [line for line in lines if str(line["event"]).startswith("app_")]


async def _sends_an_unknown_lifespan_message(scope: Scope, receive: Receive, send: Send) -> None:
    await receive()
    await send({"type": "lifespan.startup.unknown"})


def test_a_lifespan_message_of_another_type_reaches_the_server_untouched() -> None:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        sent.append(message)

    asyncio.run(
        _behind_the_middleware(_sends_an_unknown_lifespan_message)(
            {"type": "lifespan"}, receive, send
        )
    )

    assert sent == [{"type": "lifespan.startup.unknown"}]
