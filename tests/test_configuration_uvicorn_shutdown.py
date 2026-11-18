import asyncio
import socket

import pytest
import uvicorn
from websockets.asyncio.client import connect

from logline import configure
from logline.standard import BASE_KEYS
from tests.support.lines import Line, json_lines, lines_of
from tests.support.raw_app import HELD_PATH, HELD_SOCKET_PATH, RawApp
from tests.support.serving import HTTP_IMPLEMENTATIONS, WrittenEvents

_UVICORN_LOGGER = "uvicorn.error"
_WAITING = "server_shutdown.waiting"


async def _start_held_request(base_url: str, app: RawApp) -> asyncio.StreamWriter:
    host, port = base_url.removeprefix("http://").split(":")
    _, writer = await asyncio.open_connection(host, int(port))
    writer.write(f"GET {HELD_PATH} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
    await writer.drain()
    await app.entered.wait()
    return writer


def _stop_with_work_left(
    http: str, *, open_websocket: bool, timeout_graceful_shutdown: int | None
) -> None:
    """Stop a server that still has a background task and, if asked, an open WebSocket."""

    async def run() -> None:
        app = RawApp()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        config = uvicorn.Config(
            app,
            log_config=None,
            lifespan="off",
            http=http,
            timeout_graceful_shutdown=timeout_graceful_shutdown,
        )
        server = uvicorn.Server(config)
        with WrittenEvents(_UVICORN_LOGGER) as written:
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                await _hold_and_stop(server, app, written, base_url, open_websocket)
            finally:
                app.release.set()
                await serving
                listener.close()

    asyncio.run(run())


async def _hold_and_stop(
    server: uvicorn.Server,
    app: RawApp,
    written: WrittenEvents,
    base_url: str,
    open_websocket: bool,
) -> None:
    held_connection = await _start_held_request(base_url, app)
    if not open_websocket:
        held_connection.close()
        server.should_exit = True
        await written.seen("server_shutdown.timed_out").wait()
        return
    async with connect(base_url.replace("http", "ws") + HELD_SOCKET_PATH):
        server.should_exit = True
        await written.seen(_WAITING, "connections").wait()
        held_connection.close()
        await written.seen(_WAITING, "background_tasks").wait()


def _server_lines(output: str) -> list[Line]:
    return lines_of(json_lines(output), _UVICORN_LOGGER)


@pytest.mark.parametrize("http", HTTP_IMPLEMENTATIONS)
def test_a_graceful_stop_with_an_open_websocket_and_a_background_task_logs_only_literal_lines(
    capsys: pytest.CaptureFixture[str], http: str
) -> None:
    configure("uvicorn-service")

    _stop_with_work_left(http, open_websocket=True, timeout_graceful_shutdown=None)

    lines = _server_lines(capsys.readouterr().out)
    assert [(line["level"], line["event"]) for line in lines if line["event"] == _WAITING] == [
        ("info", _WAITING),
        ("info", _WAITING),
    ]
    assert [line["reason"] for line in lines if line["event"] == _WAITING] == [
        "connections",
        "background_tasks",
    ]
    assert all(line.keys() <= {*BASE_KEYS, "reason"} for line in lines)
    assert {line["event"] for line in lines} == {
        "server_process.started",
        "server_shutdown.started",
        _WAITING,
        "server_process.finished",
    }


@pytest.mark.parametrize("http", HTTP_IMPLEMENTATIONS)
def test_a_stop_that_outlasts_the_graceful_timeout_logs_the_cancelled_task_count(
    capsys: pytest.CaptureFixture[str], http: str
) -> None:
    configure("uvicorn-service")

    _stop_with_work_left(http, open_websocket=False, timeout_graceful_shutdown=1)

    (line,) = [
        line
        for line in _server_lines(capsys.readouterr().out)
        if line["event"] == "server_shutdown.timed_out"
    ]
    assert line["level"] == "error"
    assert line["task_count"] == 1
