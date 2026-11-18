import asyncio
import json
import logging
import os
import socket
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from logline import configure
from logline.lifespan import LIFESPAN_LOGGER_NAME
from logline.standard import BASE_KEYS
from tests.support.apps import (
    WEBSOCKET_DENIED_PATH,
    WEBSOCKET_DENIED_STATUS,
    build_app,
)
from tests.support.lines import Line, json_lines, lines_of
from tests.support.serving import serve

_UVICORN_LOGGER = "uvicorn.error"
_LOCAL_HOST = "127.0.0.1"
# TEST-NET-1 (RFC 5737): no local interface owns it, so binding fails without any network.
_UNOWNED_HOST = "203.0.113.1"
_REQUEST_SECRET = "request-secret"


def _coded_lines(output: str, event_prefix: str) -> list[Line]:
    return [
        line
        for line in lines_of(json_lines(output), _UVICORN_LOGGER)
        if str(line["event"]).startswith(event_prefix)
    ]


def _lines_at(output: str, level: str) -> list[Line]:
    lines = lines_of(json_lines(output), _UVICORN_LOGGER)
    return [line for line in lines if line["level"] == level]


def _the_one_line(output: str, event: str) -> Line:
    (line,) = [line for line in _coded_lines(output, event) if line["event"] == event]
    return line


def _content_of(line: Line) -> str:
    """The line without its timestamp and traceback, which no host, port or path should reach."""
    return json.dumps({key: value for key, value in line.items() if key not in {"ts", "exception"}})


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

    output = capsys.readouterr().out
    line = _the_one_line(output, str(expected["event"]))
    assert len(_lines_at(output, level)) == 1
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


@pytest.mark.parametrize(
    ("message", "arguments", "event"),
    [
        pytest.param(
            "Something else happened: %s", ("detail",), "Something else happened: detail", id="text"
        ),
        pytest.param(ValueError("detail"), (), "detail", id="object"),
        pytest.param({"detail": "value"}, (), "{'detail': 'value'}", id="unhashable object"),
    ],
)
def test_an_unknown_uvicorn_record_passes_unchanged(
    capsys: pytest.CaptureFixture[str],
    message: object,
    arguments: tuple[str, ...],
    event: str,
) -> None:
    configure("uvicorn-service")

    async def exercise(_base_url: str) -> None:
        logging.getLogger(_UVICORN_LOGGER).warning(message, *arguments)

    serve(uvicorn.Config(build_app(), log_config=None), exercise)

    line = _the_one_line(capsys.readouterr().out, event)
    assert line["level"] == "warning"
    assert not {"reason", "error_type"} & line.keys()


def _malformed_upgrade(implementation: str) -> None:
    async def exercise(base_url: str) -> None:
        host, port = base_url.removeprefix("http://").split(":")
        reader, writer = await asyncio.open_connection(host, int(port))
        # An upgrade without its `Sec-WebSocket-Key`, which websockets answers before any app task.
        writer.write(
            f"GET {WEBSOCKET_DENIED_PATH} HTTP/1.1\r\nHost: {host}\r\nConnection: Upgrade\r\n"
            "Upgrade: websocket\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        await writer.drain()
        assert (await reader.readline()).startswith(b"HTTP/1.1 400")
        writer.close()

    serve(uvicorn.Config(build_app(), log_config=None, ws=implementation), exercise)


def _websocket_lines(output: str) -> list[tuple[str, str, object]]:
    """The `(level, event, status)` of each WebSocket line of the server, in the order written."""
    return [
        (str(line["level"]), str(line["event"]), line.get("status"))
        for line in lines_of(json_lines(output), _UVICORN_LOGGER)
        if str(line["event"]).startswith("websocket_")
    ]


_REFUSED = "websocket_upgrade.refused"
_MALFORMED_UPGRADE_STATUS = 400


def test_a_malformed_websocket_upgrade_logs_one_literal_refusal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("uvicorn-service")

    _malformed_upgrade("websockets-sansio")

    output = capsys.readouterr().out
    assert _websocket_lines(output) == [("info", _REFUSED, _MALFORMED_UPGRADE_STATUS)]
    (line,) = _coded_lines(output, "websocket_")
    assert line.keys() <= {*BASE_KEYS, "status"}
    assert "Bad Request" not in _content_of(line)


def test_a_denied_upgrade_of_uvicorns_older_websockets_implementation_logs_its_refusal_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("uvicorn-service")

    async def exercise(base_url: str) -> None:
        with pytest.raises(InvalidHandshake):
            await connect(base_url.replace("http", "ws") + WEBSOCKET_DENIED_PATH)

    serve(uvicorn.Config(build_app(), log_config=None, ws="websockets"), exercise)

    output = capsys.readouterr().out
    assert _websocket_lines(output) == [("info", _REFUSED, WEBSOCKET_DENIED_STATUS)]


def test_a_websocket_refusal_below_the_root_level_writes_no_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "warning")
    configure("uvicorn-service")

    _malformed_upgrade("websockets-sansio")

    assert lines_of(json_lines(capsys.readouterr().out), _UVICORN_LOGGER) == []


def _start_and_stop(config: uvicorn.Config) -> None:
    server = uvicorn.Server(config)
    # A server told to exit before it serves still starts up and shuts down.
    server.should_exit = True
    try:
        asyncio.run(server.serve())
    finally:
        if config.fd is not None:
            # uvicorn duplicates the descriptor it is given, so the original stays open.
            os.close(config.fd)


def _listening_on_a_port(_directory: Path) -> uvicorn.Config:
    return uvicorn.Config(build_app(), host=_LOCAL_HOST, port=0, log_config=None)


def _listening_on_a_unix_socket(directory: Path) -> uvicorn.Config:
    return uvicorn.Config(build_app(), uds=str(directory / "server.sock"), log_config=None)


def _listening_on_a_socket_descriptor(directory: Path) -> uvicorn.Config:
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(directory / "descriptor.sock"))
    listener.set_inheritable(True)
    return uvicorn.Config(build_app(), fd=listener.detach(), log_config=None)


def _listening_with_the_lifespan_off(_directory: Path) -> uvicorn.Config:
    return uvicorn.Config(build_app(), host=_LOCAL_HOST, port=0, lifespan="off", log_config=None)


_LIFECYCLE_EVENTS = [
    "server_process.started",
    "app_startup.completed",
    "server_listen.started",
    "server_shutdown.started",
    "app_shutdown.completed",
    "server_process.finished",
]
_LIFECYCLE_EVENTS_WITHOUT_THE_LIFESPAN = [
    event for event in _LIFECYCLE_EVENTS if not event.startswith("app_")
]


def _uvicorn_lines(output: str) -> list[Line]:
    """The lines of uvicorn and of the lifespan, in the order written."""
    return [
        line
        for line in json_lines(output)
        if line["logger"] in {_UVICORN_LOGGER, LIFESPAN_LOGGER_NAME}
    ]


@pytest.mark.parametrize(
    ("config_for", "events"),
    [
        pytest.param(_listening_on_a_port, _LIFECYCLE_EVENTS, id="port"),
        pytest.param(_listening_on_a_unix_socket, _LIFECYCLE_EVENTS, id="unix socket"),
        pytest.param(_listening_on_a_socket_descriptor, _LIFECYCLE_EVENTS, id="socket descriptor"),
        pytest.param(
            _listening_with_the_lifespan_off,
            _LIFECYCLE_EVENTS_WITHOUT_THE_LIFESPAN,
            id="lifespan off",
        ),
    ],
)
def test_a_start_and_stop_logs_only_the_literal_lifecycle_events(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    config_for: Callable[[Path], uvicorn.Config],
    events: list[str],
) -> None:
    configure("uvicorn-service")

    _start_and_stop(config_for(tmp_path))

    lines = _uvicorn_lines(capsys.readouterr().out)
    assert [(line["level"], line["event"]) for line in lines] == [
        ("info", event) for event in events
    ]
    for line in lines:
        assert line.keys() == BASE_KEYS


def test_a_second_configure_leaves_the_uvicorn_lines_as_they_were(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("uvicorn-service")
    configure("uvicorn-service")

    _malformed_upgrade("websockets-sansio")

    lines = _uvicorn_lines(capsys.readouterr().out)
    assert [(line["level"], line["event"]) for line in lines] == [
        ("info", "server_process.started"),
        ("info", "app_startup.completed"),
        ("info", _REFUSED),
        ("info", "server_shutdown.started"),
        ("info", "app_shutdown.completed"),
        ("info", "server_process.finished"),
    ]


def test_a_record_without_exc_info_and_with_odd_args_passes_the_filter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("uvicorn-service")
    uvicorn_logger = logging.getLogger(_UVICORN_LOGGER)

    for template in ("Exception in ASGI application\n", "connection rejected (%d %s)"):
        uvicorn_logger.handle(
            logging.LogRecord(_UVICORN_LOGGER, logging.ERROR, __file__, 0, template, (), None)
        )

    lines = lines_of(json_lines(capsys.readouterr().out), _UVICORN_LOGGER)
    assert [line["event"] for line in lines] == ["asgi_app.failed", "websocket_upgrade.refused"]
    assert not {"error_type", "status"} & {key for line in lines for key in line}


def test_a_websocket_under_the_debug_level_never_enables_uvicorn_debug_records(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    configure("uvicorn-service")

    _malformed_upgrade("websockets-sansio")

    assert not logging.getLogger("uvicorn.error").isEnabledFor(logging.DEBUG)
    assert _the_one_line(capsys.readouterr().out, _REFUSED)["level"] == "info"
