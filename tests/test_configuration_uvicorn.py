import logging
from collections.abc import Callable

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import connect

from logline import configure
from tests.support.apps import build_app
from tests.support.lines import BASE_KEYS, Line, json_lines, lines_of
from tests.support.serving import serve

_WEBSOCKET_QUERY_SECRET = "websocket-query-secret"


async def _get_items(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url) as client:
        await client.get("/items/1")
        await client.get("/library-line")


def _uvicorn_default_config() -> uvicorn.Config:
    return uvicorn.Config(build_app())


def test_uvicorn_and_library_lines_are_the_same_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure("uvicorn-service")

    serve(uvicorn.Config(build_app(), log_config=None), _get_items)

    lines = json_lines(capsys.readouterr().out)
    startup = [
        line
        for line in lines_of(lines, "uvicorn.error")
        if "startup complete" in str(line["event"])
    ]
    library = lines_of(lines, "some_library")
    assert startup
    assert [line["event"] for line in library] == ["library says hello"]
    for line in [*startup, *library]:
        assert BASE_KEYS <= line.keys()
        assert line["service"] == "uvicorn-service"


@pytest.mark.parametrize(
    "make_config",
    [
        pytest.param(lambda: uvicorn.Config(build_app(), log_config=None), id="log_config=None"),
        pytest.param(_uvicorn_default_config, id="default config after configure"),
    ],
)
def test_uvicorns_access_log_stays_silent(
    capsys: pytest.CaptureFixture[str], make_config: Callable[[], uvicorn.Config]
) -> None:
    configure("uvicorn-service")

    serve(make_config(), _get_items)

    captured = capsys.readouterr()
    assert "HTTP/1.1" not in captured.out + captured.err


def test_the_access_log_stays_silent_when_configure_runs_after_uvicorns_default_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _uvicorn_default_config()
    configure("uvicorn-service")

    serve(config, _get_items)

    captured = capsys.readouterr()
    assert "HTTP/1.1" not in captured.out + captured.err
    lines = json_lines(captured.out)
    loggers = {line["logger"] for line in lines}
    assert {"uvicorn.error", "some_library", "logline.request"} <= loggers


def test_log_level_silences_uvicorn_lines_after_its_default_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "warning")
    config = _uvicorn_default_config()
    configure("uvicorn-service")

    serve(config, _get_items)

    lines = json_lines(capsys.readouterr().out)
    assert lines_of(lines, "uvicorn.error") == []


def test_uvicorns_websocket_line_never_appears(capsys: pytest.CaptureFixture[str]) -> None:
    configure("uvicorn-service")

    async def exercise(base_url: str) -> None:
        url = base_url.replace("http", "ws") + f"/ws?token={_WEBSOCKET_QUERY_SECRET}"
        async with connect(url) as websocket:
            assert await websocket.recv() == "hello"

    serve(uvicorn.Config(build_app(), log_config=None, ws="websockets-sansio"), exercise)

    output = capsys.readouterr().out
    lines: list[Line] = json_lines(output)
    assert lines_of(lines, "uvicorn.error")
    assert "WebSocket" not in output
    assert _WEBSOCKET_QUERY_SECRET not in output
    assert "/ws" not in output


def test_a_websocket_connection_gets_no_request_line(capsys: pytest.CaptureFixture[str]) -> None:
    configure("uvicorn-service")

    async def exercise(base_url: str) -> None:
        async with connect(base_url.replace("http", "ws") + "/ws") as websocket:
            assert await websocket.recv() == "hello"

    serve(uvicorn.Config(build_app(), log_config=None, ws="websockets-sansio"), exercise)

    assert lines_of(json_lines(capsys.readouterr().out), "logline.request") == []


@pytest.mark.parametrize("logger_name", ["httpx", "httpx2", "httpcore"])
@pytest.mark.parametrize("level", [None, "debug"])
def test_a_http_client_info_line_with_a_url_never_appears(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    logger_name: str,
    level: str | None,
) -> None:
    if level is not None:
        monkeypatch.setenv("LOG_LEVEL", level)
    configure("clients")

    logging.getLogger(logger_name).info('HTTP Request: GET http://host/path?query=secret "200 OK"')

    assert capsys.readouterr().out == ""


def test_a_real_httpx_call_logs_no_line_with_its_url(capsys: pytest.CaptureFixture[str]) -> None:
    configure("clients")

    async def exercise(base_url: str) -> None:
        async with httpx.AsyncClient() as client:
            await client.get(f"{base_url}/items/1?query=client-query-secret")

    serve(uvicorn.Config(build_app(), log_config=None), exercise)

    output = capsys.readouterr().out
    assert "client-query-secret" not in output
    assert not lines_of(json_lines(output), "httpx")
