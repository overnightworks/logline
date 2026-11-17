import asyncio
import contextlib
import json
import logging
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from logline import configure
from tests.support.apps import build_app
from tests.support.lines import BASE_KEYS, Line, json_lines, lines_of
from tests.support.serving import serve

_UVICORN_LOGGER = "uvicorn.error"
_LOCAL_HOST = "127.0.0.1"
# TEST-NET-1 (RFC 5737): no local interface owns it, so binding fails without any network.
_UNOWNED_HOST = "203.0.113.1"
_FAILURE_MESSAGE = "lifespan failure message"
_REQUEST_SECRET = "request-secret"


def _coded_lines(output: str, event_prefix: str) -> list[Line]:
    return [
        line
        for line in lines_of(json_lines(output), _UVICORN_LOGGER)
        if str(line["event"]).startswith(event_prefix)
    ]


def _the_one_line(output: str, event: str) -> Line:
    (line,) = [line for line in _coded_lines(output, event) if line["event"] == event]
    return line


def _content_of(line: Line) -> str:
    """The line without its timestamp and traceback, which no host, port or path should reach."""
    return json.dumps({key: value for key, value in line.items() if key not in {"ts", "exception"}})


def _lifespan_app(*, fail_at_startup: bool) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if fail_at_startup:
            raise RuntimeError(_FAILURE_MESSAGE)
        yield
        raise RuntimeError(_FAILURE_MESSAGE)

    return FastAPI(lifespan=lifespan)


async def _nothing(_base_url: str) -> None:
    return None


def _serve_without_requests(app: FastAPI) -> None:
    serve(uvicorn.Config(app, log_config=None), _nothing)


def _serve_bind_failure(host: str, port: int) -> None:
    server = uvicorn.Server(uvicorn.Config(build_app(), host=host, port=port, log_config=None))
    serving = server.serve()
    with pytest.raises(SystemExit):
        asyncio.run(serving)


def _occupied_port_failure() -> tuple[str, ...]:
    with socket.socket() as occupant:
        occupant.bind((_LOCAL_HOST, 0))
        occupant.listen()
        port = occupant.getsockname()[1]
        _serve_bind_failure(_LOCAL_HOST, port)
    return (str(port),)


def _unowned_address_failure() -> tuple[str, ...]:
    _serve_bind_failure(_UNOWNED_HOST, 0)
    return ()


def _malformed_request() -> tuple[str, ...]:
    async def exercise(base_url: str) -> None:
        host, port = base_url.removeprefix("http://").split(":")
        reader, writer = await asyncio.open_connection(host, int(port))
        writer.write(f"not an http request {_REQUEST_SECRET}\r\n\r\n".encode())
        await writer.drain()
        assert (await reader.readline()).startswith(b"HTTP/1.1 400")
        writer.close()

    serve(uvicorn.Config(build_app(), log_config=None), exercise)
    return ()


def _unsupported_upgrade() -> tuple[str, ...]:
    async def exercise(base_url: str) -> None:
        async with httpx.AsyncClient(base_url=base_url) as client:
            await client.get("/items/1", headers={"Connection": "upgrade", "Upgrade": "h2c"})

    serve(uvicorn.Config(build_app(), log_config=None), exercise)
    return ()


def _http_app_exception() -> tuple[str, ...]:
    async def exercise(base_url: str) -> None:
        async with httpx.AsyncClient(base_url=base_url) as client:
            answer = await client.get("/boom", headers={"x-secret": _REQUEST_SECRET})
            assert answer.status_code == 500

    serve(uvicorn.Config(build_app(), log_config=None), exercise)
    return ()


def _websocket_app_exception(implementation: str) -> Callable[[], tuple[str, ...]]:
    def drive() -> tuple[str, ...]:
        async def exercise(base_url: str) -> None:
            async with connect(base_url.replace("http", "ws") + "/ws-boom") as websocket:
                with pytest.raises(ConnectionClosed):
                    await websocket.recv()

        serve(uvicorn.Config(build_app(), log_config=None, ws=implementation), exercise)
        return ()

    return drive


def _startup_failure() -> tuple[str, ...]:
    app = _lifespan_app(fail_at_startup=True)
    # Recent uvicorn exits the process on a failed startup; older releases return from `serve`.
    with contextlib.suppress(SystemExit):
        _serve_without_requests(app)
    return ()


def _shutdown_failure() -> tuple[str, ...]:
    _serve_without_requests(_lifespan_app(fail_at_startup=False))
    return ()


@pytest.mark.parametrize(
    ("drive", "level", "expected", "has_traceback"),
    [
        pytest.param(
            _occupied_port_failure,
            "error",
            {"event": "server_bind.failed", "reason": "port_taken", "error_type": "OSError"},
            False,
            id="occupied port",
        ),
        pytest.param(
            _unowned_address_failure,
            "error",
            {"event": "server_bind.failed", "reason": "os_error", "error_type": "OSError"},
            False,
            id="other bind error",
        ),
        pytest.param(
            _malformed_request,
            "warning",
            {"event": "http_request.refused", "reason": "invalid_request"},
            False,
            id="malformed request",
        ),
        pytest.param(
            _unsupported_upgrade,
            "warning",
            {"event": "http_request.refused", "reason": "unsupported_upgrade"},
            False,
            id="unsupported upgrade",
        ),
        pytest.param(
            _http_app_exception,
            "error",
            {"event": "asgi_app.failed", "error_type": "RuntimeError"},
            True,
            id="http app exception",
        ),
        pytest.param(
            _websocket_app_exception("websockets"),
            "error",
            {"event": "asgi_app.failed", "error_type": "RuntimeError"},
            True,
            id="websocket app exception, websockets",
        ),
        pytest.param(
            _websocket_app_exception("websockets-sansio"),
            "error",
            {"event": "asgi_app.failed", "error_type": "RuntimeError"},
            True,
            id="websocket app exception, websockets-sansio",
        ),
        pytest.param(
            _startup_failure, "error", {"event": "app_startup.failed"}, False, id="startup"
        ),
        pytest.param(
            _shutdown_failure, "error", {"event": "app_shutdown.failed"}, False, id="shutdown"
        ),
    ],
)
def test_a_uvicorn_failure_logs_one_literal_event_with_its_code(
    capsys: pytest.CaptureFixture[str],
    drive: Callable[[], tuple[str, ...]],
    level: str,
    expected: Line,
    has_traceback: bool,
) -> None:
    configure("uvicorn-service")

    forbidden_in_content = drive()

    line = _the_one_line(capsys.readouterr().out, str(expected["event"]))
    assert line["level"] == level
    assert expected.items() <= line.items()
    assert line.keys() <= {*BASE_KEYS, "reason", "error_type", "exception"}
    if has_traceback:
        assert isinstance(line["exception"], str)
        assert line["exception"].startswith("Traceback (most recent call last):")
        assert "RuntimeError" in line["exception"]
    content = _content_of(line)
    for forbidden in (
        *forbidden_in_content,
        _LOCAL_HOST,
        _UNOWNED_HOST,
        "Errno",
        "address",
        "Invalid HTTP request",
        "Unsupported upgrade",
        "Application",
        "Exception in",
        _REQUEST_SECRET,
    ):
        assert forbidden not in content


def test_an_unknown_uvicorn_record_passes_unchanged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("uvicorn-service")

    async def exercise(_base_url: str) -> None:
        logging.getLogger(_UVICORN_LOGGER).warning("Something else happened: %s", "detail")

    serve(uvicorn.Config(build_app(), log_config=None), exercise)

    line = _the_one_line(capsys.readouterr().out, "Something else happened: detail")
    assert line["level"] == "warning"
    assert not {"reason", "error_type"} & line.keys()
