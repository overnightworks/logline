import asyncio
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from logline import (
    REQUEST_EVENT,
    Outcome,
    RequestLineMiddleware,
    configure,
    set_client_ip,
    set_route,
    set_sub,
)
from logline.request import REQUEST_LOGGER_NAME
from tests.support.apps import (
    BACKGROUND_LOGGER,
    CANCELLED_PATH,
    CHAIN_CAUSE_MESSAGE,
    CHAIN_MESSAGE,
    EMPTY_ROUTE_PATH,
    GATED_PATH,
    GROUP_MEMBER_MESSAGES,
    HOST_CLIENT_IP,
    HOST_ROUTE,
    MCP_ROUTE,
    MOUNTED_APP,
    NAMED_STATIC_MOUNT,
    PLAIN_ROUTE,
    STATIC_MOUNT,
    SUB,
    UNAVAILABLE_PATH,
    build_app,
)
from tests.support.lines import json_lines, lines_of, request_lines
from tests.support.serving import Call, get, serve_requests

# Twelve digits: the timestamps, process ids and trace ids in the output cannot hold them by chance.
_UNGUESSABLE_ID = "918273645012"
_LOGGERS_OF_THE_REQUEST = {REQUEST_LOGGER_NAME, BACKGROUND_LOGGER}


@pytest.mark.parametrize(
    ("sent", "logged"),
    [
        ("GET", "GET"),
        ("DELETE", "DELETE"),
        ("BREW", "_OTHER"),
        ("PROPFIND", "_OTHER"),
    ],
)
def test_the_method_of_a_line_is_a_standard_method_or_other(
    capsys: pytest.CaptureFixture[str], sent: str, logged: str
) -> None:
    _, lines = serve_requests(capsys, [Call(PLAIN_ROUTE, method=sent)])

    assert request_lines(lines)[0]["method"] == logged


@pytest.mark.parametrize(
    ("path", "route", "status"),
    [
        pytest.param(f"/items/{_UNGUESSABLE_ID}", "/items/{item_id}", 200, id="api-route"),
        pytest.param("/async-route", HOST_ROUTE, 200, id="api-route-replaced-by-set-route"),
        pytest.param("/reset-route", HOST_ROUTE, 200, id="last-set-route-wins"),
        pytest.param(EMPTY_ROUTE_PATH, None, 200, id="api-route-given-an-empty-template"),
        pytest.param(MCP_ROUTE, MCP_ROUTE, 200, id="plain-route-naming-itself"),
        pytest.param(PLAIN_ROUTE, None, 200, id="plain-route"),
        pytest.param(f"{MOUNTED_APP}/{_UNGUESSABLE_ID}", "/{name}", 200, id="mounted-app-route"),
        pytest.param(f"{STATIC_MOUNT}/{_UNGUESSABLE_ID}.txt", None, 200, id="static-mount"),
        pytest.param(
            f"{NAMED_STATIC_MOUNT}/{_UNGUESSABLE_ID}.txt",
            NAMED_STATIC_MOUNT,
            200,
            id="static-mount-naming-itself",
        ),
        pytest.param(f"/{_UNGUESSABLE_ID}", None, 404, id="no-route"),
        pytest.param(GATED_PATH, None, 401, id="gate-answering-401"),
    ],
)
def test_the_route_of_a_line_is_the_template_the_framework_or_host_left_and_never_the_path(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, path: str, route: str | None, status: int
) -> None:
    (tmp_path / f"{_UNGUESSABLE_ID}.txt").write_text("static content")

    output, lines = serve_requests(
        capsys,
        [get(f"{path}?search=private-query")],
        config=uvicorn.Config(build_app(static_directory=tmp_path), log_config=None),
    )

    (line,) = request_lines(lines)
    assert line["event"] == REQUEST_EVENT
    assert line["method"] == "GET"
    assert (line["route"], line["status"]) == (route, status)
    assert isinstance(line["duration_ms"], float)
    assert line["duration_ms"] >= 0
    assert _UNGUESSABLE_ID not in output
    assert "private-query" not in output


@pytest.mark.parametrize(
    ("endpoint", "below_base_http_middleware"),
    [("/sync-route", False), ("/async-route", True), ("/sync-route", True)],
)
def test_a_route_set_in_an_endpoint_reaches_the_line(
    capsys: pytest.CaptureFixture[str], endpoint: str, below_base_http_middleware: bool
) -> None:
    app = build_app(below_base_http_middleware=below_base_http_middleware)

    _, lines = serve_requests(capsys, [get(endpoint)], config=uvicorn.Config(app, log_config=None))

    (line,) = request_lines(lines)
    assert line["route"] == HOST_ROUTE


def test_a_request_whose_endpoint_raised_logs_500_and_a_traceback_without_its_locals(
    capsys: pytest.CaptureFixture[str],
) -> None:
    output, lines = serve_requests(capsys, [get("/boom", **{"x-secret": "secret-in-a-local"})])

    (line,) = request_lines(lines)
    assert line["route"] == "/boom"
    assert line["status"] == 500
    (failure,) = [line for line in lines_of(lines, "uvicorn.error") if line["level"] == "error"]
    assert "RuntimeError: boom after reading 17 characters" in str(failure["exception"])
    assert "secret-in-a-local" not in output


def test_the_request_event_is_the_past_tense_name_the_standard_ruled() -> None:
    assert REQUEST_EVENT == "request.ended"


@pytest.mark.parametrize(
    ("path", "status", "outcome"),
    [
        pytest.param("/items/1", 200, Outcome.OK, id="success"),
        pytest.param("/missing", 404, Outcome.OK, id="client-error-status"),
        pytest.param(GATED_PATH, 401, Outcome.OK, id="gate-refusal-status"),
        pytest.param(UNAVAILABLE_PATH, 503, Outcome.FAILED, id="server-error-status"),
        pytest.param("/boom", 500, Outcome.FAILED, id="app-raised-an-exception"),
        pytest.param(CANCELLED_PATH, 500, Outcome.CANCELLED, id="app-raised-a-cancellation"),
    ],
)
def test_the_request_line_names_how_the_request_ended(
    capsys: pytest.CaptureFixture[str], path: str, status: int, outcome: Outcome
) -> None:
    _, lines = serve_requests(capsys, [get(path, **{"x-secret": "value"})])

    (line,) = request_lines(lines)
    assert (line["status"], line["outcome"]) == (status, outcome.value)


@pytest.mark.parametrize("path", ["/chained", "/chained-sync"])
def test_an_endpoint_raising_from_a_cause_logs_both_types_and_messages_in_one_text(
    capsys: pytest.CaptureFixture[str], path: str
) -> None:
    _, lines = serve_requests(capsys, [get(path)])

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
    _, lines = serve_requests(capsys, [get("/group")])

    (failure,) = [line for line in lines_of(lines, "uvicorn.error") if line["level"] == "error"]
    text = str(failure["exception"])
    first, second = GROUP_MEMBER_MESSAGES
    assert f"ValueError: {first}" in text
    assert f"TypeError: {second}" in text


def test_the_request_line_is_written_when_the_response_ends_not_after_background_work(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = serve_requests(capsys, [get("/background-task")])

    written = [line["logger"] for line in lines if line["logger"] in _LOGGERS_OF_THE_REQUEST]
    assert written == [REQUEST_LOGGER_NAME, BACKGROUND_LOGGER]


def test_an_exception_after_the_response_ended_leaves_the_line_the_response_wrote(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = serve_requests(capsys, [get("/background-task-raising")])

    (line,) = request_lines(lines)
    assert (line["status"], line["outcome"]) == (200, Outcome.OK.value)


def test_a_streamed_response_gets_one_line_when_its_last_chunk_is_sent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = serve_requests(capsys, [get("/stream")])

    (line,) = request_lines(lines)
    assert line["route"] == "/stream"
    assert line["status"] == 200


def test_each_request_gets_exactly_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    _, lines = serve_requests(
        capsys, [get("/items/1"), get("/items/2"), get("/missing"), get("/async-sub")]
    )

    assert [line["status"] for line in request_lines(lines)] == [200, 200, 404, 200]


@pytest.mark.parametrize("below_base_http_middleware", [False, True])
@pytest.mark.parametrize("endpoint", ["/async-sub", "/sync-sub"])
def test_a_sub_set_in_an_endpoint_reaches_the_line(
    capsys: pytest.CaptureFixture[str], endpoint: str, below_base_http_middleware: bool
) -> None:
    app = build_app(below_base_http_middleware=below_base_http_middleware)

    _, lines = serve_requests(capsys, [get(endpoint)], config=uvicorn.Config(app, log_config=None))

    (line,) = request_lines(lines)
    assert line["sub"] == SUB


def test_a_request_without_a_known_person_logs_a_null_sub(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = serve_requests(capsys, [get("/items/1")])

    (line,) = request_lines(lines)
    assert line["sub"] is None


def test_the_ip_is_the_asgi_client_until_the_host_sets_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = serve_requests(capsys, [get("/items/1"), get("/host-client-ip")])

    default, set_by_host = request_lines(lines)
    assert default["ip"] == "127.0.0.1"
    assert set_by_host["ip"] == HOST_CLIENT_IP


def test_behind_a_trusted_proxy_the_ip_is_the_forwarded_client(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = uvicorn.Config(
        build_app(), log_config=None, proxy_headers=True, forwarded_allow_ips="127.0.0.1"
    )

    _, lines = serve_requests(
        capsys, [get("/items/1", **{"x-forwarded-for": "203.0.113.9"})], config=config
    )

    (line,) = request_lines(lines)
    assert line["ip"] == "203.0.113.9"


def test_no_credential_header_query_or_body_reaches_any_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    call = Call(
        "/body?api_key=secret-query",
        method="POST",
        headers={"authorization": "Bearer secret-token", "cookie": "session=secret-cookie"},
        content=b"secret-body",
    )

    output, lines = serve_requests(capsys, [call])

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


@pytest.mark.parametrize(
    "report",
    [
        pytest.param(lambda scope: set_sub(scope, SUB), id="sub"),
        pytest.param(lambda scope: set_client_ip(scope, HOST_CLIENT_IP), id="client ip"),
        pytest.param(lambda scope: set_route(scope, HOST_ROUTE), id="route"),
    ],
)
def test_a_host_report_outside_the_middleware_changes_nothing(
    report: Callable[[dict[str, Any]], None],
) -> None:
    scope: dict[str, Any] = {"type": "http"}

    report(scope)

    assert scope == {"type": "http"}


def test_a_scope_of_another_type_reaches_the_app_without_a_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("request-service")
    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    _run_http_scope(app, {"type": "unknown"})

    assert (seen, capsys.readouterr().out) == (["unknown"], "")
