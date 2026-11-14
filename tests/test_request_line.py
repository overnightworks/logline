import asyncio
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from logline import RequestLineMiddleware, configure, set_client_ip, set_sub
from logline.request import REQUEST_LOGGER_NAME
from tests.support.apps import (
    BACKGROUND_LOGGER,
    CHAIN_CAUSE_MESSAGE,
    CHAIN_MESSAGE,
    GATED_PATH,
    GROUP_MEMBER_MESSAGES,
    HOST_CLIENT_IP,
    STATIC_FILE_NAME,
    STATIC_MOUNT,
    SUB,
    SUB_APP_MOUNT,
    build_app,
    build_mounted_app,
)
from tests.support.lines import Line, json_lines, lines_of, request_lines
from tests.support.serving import serve

# Twelve digits: the timestamps, process ids and trace ids in the output cannot hold them by chance.
_UNGUESSABLE_ID = "918273645012"
_LOGGERS_OF_THE_REQUEST = {REQUEST_LOGGER_NAME, BACKGROUND_LOGGER}


@dataclass(frozen=True)
class _Call:
    path: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""


def _get(path: str, **headers: str) -> _Call:
    return _Call(path, headers=headers)


def _serve_requests(
    capsys: pytest.CaptureFixture[str],
    calls: list[_Call],
    *,
    config: uvicorn.Config | None = None,
) -> tuple[str, list[Line]]:
    """Serve the app, make `calls` one after another, return stdout and its parsed lines."""
    configure("request-service")

    async def exercise(base_url: str) -> None:
        async with httpx.AsyncClient(base_url=base_url) as client:
            for call in calls:
                await client.request(
                    call.method, call.path, headers=call.headers, content=call.content
                )

    serve(config or uvicorn.Config(build_app(), log_config=None), exercise)
    output = capsys.readouterr().out
    return output, json_lines(output)


def test_a_request_with_a_route_logs_one_line_with_its_template_and_never_its_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    output, lines = _serve_requests(
        capsys, [_get(f"/items/{_UNGUESSABLE_ID}?search=private-query")]
    )

    (line,) = request_lines(lines)
    assert line["event"] == "request"
    assert line["method"] == "GET"
    assert line["route"] == "/items/{item_id}"
    assert line["status"] == 200
    assert isinstance(line["duration_ms"], float)
    assert line["duration_ms"] >= 0
    assert _UNGUESSABLE_ID not in output
    assert "private-query" not in output


def test_a_request_that_matched_no_route_logs_null_and_404_and_never_its_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    output, lines = _serve_requests(capsys, [_get("/no/such/place?search=private-query")])

    (line,) = request_lines(lines)
    assert line["route"] is None
    assert line["status"] == 404
    assert "no/such/place" not in output
    assert "private-query" not in output


@pytest.mark.parametrize(
    ("path", "route", "status"),
    [
        pytest.param(f"{STATIC_MOUNT}/{STATIC_FILE_NAME}", STATIC_MOUNT, 200, id="static-file"),
        pytest.param(f"{STATIC_MOUNT}/{_UNGUESSABLE_ID}.txt", STATIC_MOUNT, 404, id="static-miss"),
        pytest.param(
            f"{SUB_APP_MOUNT}/{_UNGUESSABLE_ID}", f"{SUB_APP_MOUNT}/{{x}}", 200, id="sub-app"
        ),
        pytest.param(GATED_PATH, None, 401, id="gate-in-front-of-the-router"),
        pytest.param(f"/{_UNGUESSABLE_ID}", None, 404, id="no-route"),
    ],
)
def test_a_mounted_request_logs_the_joined_template_and_never_its_path(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, path: str, route: str | None, status: int
) -> None:
    (tmp_path / STATIC_FILE_NAME).write_text("static content")

    output, lines = _serve_requests(
        capsys, [_get(path)], config=uvicorn.Config(build_mounted_app(tmp_path), log_config=None)
    )

    (line,) = request_lines(lines)
    assert (line["route"], line["status"]) == (route, status)
    assert _UNGUESSABLE_ID not in output
    assert STATIC_FILE_NAME not in output


def test_a_request_whose_endpoint_raised_logs_500_and_a_traceback_without_its_locals(
    capsys: pytest.CaptureFixture[str],
) -> None:
    output, lines = _serve_requests(capsys, [_get("/boom", **{"x-secret": "secret-in-a-local"})])

    (line,) = request_lines(lines)
    assert line["route"] == "/boom"
    assert line["status"] == 500
    (failure,) = [line for line in lines_of(lines, "uvicorn.error") if line["level"] == "error"]
    assert "RuntimeError: boom after reading 17 characters" in str(failure["exception"])
    assert "secret-in-a-local" not in output


@pytest.mark.parametrize("path", ["/chained", "/chained-sync"])
def test_an_endpoint_raising_from_a_cause_logs_both_types_and_messages_in_one_text(
    capsys: pytest.CaptureFixture[str], path: str
) -> None:
    _, lines = _serve_requests(capsys, [_get(path)])

    (line,) = request_lines(lines)
    assert line["status"] == 500
    (failure,) = [line for line in lines_of(lines, "uvicorn.error") if line["level"] == "error"]
    text = str(failure["exception"])
    assert f"KeyError: {CHAIN_CAUSE_MESSAGE!r}" in text
    assert f"ValueError: {CHAIN_MESSAGE}" in text
    assert "direct cause of the following exception" in text


def test_an_endpoint_raising_an_exception_group_logs_every_member(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = _serve_requests(capsys, [_get("/group")])

    (failure,) = [line for line in lines_of(lines, "uvicorn.error") if line["level"] == "error"]
    text = str(failure["exception"])
    first, second = GROUP_MEMBER_MESSAGES
    assert f"ValueError: {first}" in text
    assert f"TypeError: {second}" in text


def test_the_request_line_is_written_when_the_response_ends_not_after_background_work(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = _serve_requests(capsys, [_get("/background-task")])

    written = [line["logger"] for line in lines if line["logger"] in _LOGGERS_OF_THE_REQUEST]
    assert written == [REQUEST_LOGGER_NAME, BACKGROUND_LOGGER]


def test_a_streamed_response_gets_one_line_when_its_last_chunk_is_sent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = _serve_requests(capsys, [_get("/stream")])

    (line,) = request_lines(lines)
    assert line["route"] == "/stream"
    assert line["status"] == 200


def test_each_request_gets_exactly_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    _, lines = _serve_requests(
        capsys, [_get("/items/1"), _get("/items/2"), _get("/missing"), _get("/async-sub")]
    )

    assert [line["status"] for line in request_lines(lines)] == [200, 200, 404, 200]


@pytest.mark.parametrize("below_base_http_middleware", [False, True])
@pytest.mark.parametrize("endpoint", ["/async-sub", "/sync-sub"])
def test_a_sub_set_in_an_endpoint_reaches_the_line(
    capsys: pytest.CaptureFixture[str], endpoint: str, below_base_http_middleware: bool
) -> None:
    app = build_app(below_base_http_middleware=below_base_http_middleware)

    _, lines = _serve_requests(
        capsys, [_get(endpoint)], config=uvicorn.Config(app, log_config=None)
    )

    (line,) = request_lines(lines)
    assert line["sub"] == SUB


def test_a_request_without_a_known_person_logs_a_null_sub(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = _serve_requests(capsys, [_get("/items/1")])

    (line,) = request_lines(lines)
    assert line["sub"] is None


def test_the_ip_is_the_asgi_client_until_the_host_sets_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = _serve_requests(capsys, [_get("/items/1"), _get("/host-client-ip")])

    default, set_by_host = request_lines(lines)
    assert default["ip"] == "127.0.0.1"
    assert set_by_host["ip"] == HOST_CLIENT_IP


def test_behind_a_trusted_proxy_the_ip_is_the_forwarded_client(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = uvicorn.Config(
        build_app(), log_config=None, proxy_headers=True, forwarded_allow_ips="127.0.0.1"
    )

    _, lines = _serve_requests(
        capsys, [_get("/items/1", **{"x-forwarded-for": "203.0.113.9"})], config=config
    )

    (line,) = request_lines(lines)
    assert line["ip"] == "203.0.113.9"


def test_no_credential_header_query_or_body_reaches_any_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    call = _Call(
        "/body?api_key=secret-query",
        method="POST",
        headers={"authorization": "Bearer secret-token", "cookie": "session=secret-cookie"},
        content=b"secret-body",
    )

    output, lines = _serve_requests(capsys, [call])

    (line,) = request_lines(lines)
    assert line["route"] == "/body"
    for secret in ("secret-token", "secret-cookie", "secret-query", "secret-body"):
        assert secret not in output


def _run_http_scope(app: Callable[[Any, Any, Any], Awaitable[None]], scope: dict[str, Any]) -> None:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        return None

    asyncio.run(RequestLineMiddleware(app)(scope, receive, send))


def test_a_server_without_state_or_client_still_gets_a_line_with_a_null_ip(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("request-service")

    async def app(scope: Any, receive: Any, send: Any) -> None:
        set_sub(scope, SUB)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    _run_http_scope(app, {"type": "http", "method": "GET", "headers": []})

    (line,) = request_lines(json_lines(capsys.readouterr().out))
    assert (line["sub"], line["ip"], line["status"], line["route"]) == (SUB, None, 204, None)


def test_an_app_that_sent_no_response_logs_500(capsys: pytest.CaptureFixture[str]) -> None:
    configure("request-service")

    async def app(scope: Any, receive: Any, send: Any) -> None:
        set_client_ip(scope, HOST_CLIENT_IP)

    _run_http_scope(app, {"type": "http", "method": "GET", "headers": []})

    (line,) = request_lines(json_lines(capsys.readouterr().out))
    assert (line["status"], line["ip"]) == (500, HOST_CLIENT_IP)
