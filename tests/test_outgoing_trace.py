import asyncio
import http.client
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from urllib.parse import urlsplit

import fastapi
import httpx
import httpx2
import pytest
import urllib3
import uvicorn
from opentelemetry.propagate import set_global_textmap

from logline import RequestLineMiddleware, configure, outgoing_headers
from tests.support.apps import build_app
from tests.support.incoming_trace import INCOMING_TRACE_ID, INCOMING_TRACEPARENT
from tests.support.lines import json_lines
from tests.support.outgoing_clients import (
    A_CALLERS_TRACEPARENT,
    CLIENTS,
    SCOPES,
    TRACEPARENT,
    TRACESTATE,
    EchoServer,
    inside_a_request,
    inside_a_turn,
)
from tests.support.propagators import PropagatorFailure, RaisingPropagator
from tests.support.serving import serve

_HIDDEN_CLIENTS_HOST = Path(__file__).parent / "support" / "hidden_clients_host.py"
_ALL_CLIENTS = pytest.mark.parametrize("call", CLIENTS.values(), ids=CLIENTS.keys())


@pytest.fixture(autouse=True)
def configured_host() -> None:
    configure("outgoing-service")


@_ALL_CLIENTS
@pytest.mark.parametrize("scope", SCOPES.values(), ids=SCOPES.keys())
def test_a_call_inside_a_trace_carries_the_headers_outgoing_headers_gives(
    echo: EchoServer, call, scope
) -> None:
    with scope():
        expected = outgoing_headers()
        call(echo.url, {})

    assert {name: echo.last[name] for name in expected} == {
        name: [value] for name, value in expected.items()
    }
    assert TRACEPARENT in expected


@_ALL_CLIENTS
def test_a_call_outside_a_trace_carries_no_trace(echo: EchoServer, call) -> None:
    call(echo.url, {})

    assert TRACEPARENT not in echo.last
    assert TRACESTATE not in echo.last


@_ALL_CLIENTS
def test_a_call_that_carries_a_traceparent_keeps_it_and_gets_no_tracestate(
    echo: EchoServer, call
) -> None:
    with inside_a_request():
        call(echo.url, {"Traceparent": A_CALLERS_TRACEPARENT})

    assert echo.last[TRACEPARENT] == [A_CALLERS_TRACEPARENT]
    assert TRACESTATE not in echo.last


@_ALL_CLIENTS
def test_a_call_that_carries_both_headers_keeps_both(echo: EchoServer, call) -> None:
    with inside_a_request():
        call(echo.url, {TRACEPARENT: A_CALLERS_TRACEPARENT, TRACESTATE: "vendor=callers"})

    assert (echo.last[TRACEPARENT], echo.last[TRACESTATE]) == (
        [A_CALLERS_TRACEPARENT],
        ["vendor=callers"],
    )


@pytest.mark.parametrize(
    "first_call_headers",
    [{}, {TRACEPARENT: A_CALLERS_TRACEPARENT}],
    ids=["no-traceparent-first", "callers-traceparent-first"],
)
def test_two_requests_on_one_kept_alive_connection_each_carry_their_own_trace(
    echo: EchoServer, first_call_headers: dict[str, str]
) -> None:
    with urllib3.PoolManager(maxsize=1) as pool:
        with inside_a_request():
            first_expected = first_call_headers or outgoing_headers()
            pool.request("GET", echo.url, headers=first_call_headers)
        with inside_a_turn():
            second_expected = outgoing_headers()
            pool.request("GET", echo.url)

    first, second = echo.received
    assert first.client_port == second.client_port
    assert first.headers[TRACEPARENT] == [first_expected[TRACEPARENT]]
    assert second.headers[TRACEPARENT] == [second_expected[TRACEPARENT]]


def test_a_header_name_put_as_bytes_counts_as_the_callers(echo: EchoServer) -> None:
    parts = urlsplit(echo.url)
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port)
    try:
        with inside_a_request():
            connection.putrequest("GET", "/")
            connection.putheader(b"Traceparent", A_CALLERS_TRACEPARENT.encode())
            connection.endheaders()
            connection.getresponse().read()
    finally:
        connection.close()

    assert echo.last[TRACEPARENT] == [A_CALLERS_TRACEPARENT]


@_ALL_CLIENTS
def test_with_otel_propagators_none_a_call_carries_no_trace(
    host_with_otel_propagators_none: None, echo: EchoServer, call
) -> None:
    configure("outgoing-service")

    with inside_a_request():
        call(echo.url, {})

    assert TRACEPARENT not in echo.last


@_ALL_CLIENTS
def test_configuring_twice_still_sends_one_header_and_writes_no_line_above_info(
    capsys: pytest.CaptureFixture[str], echo: EchoServer, call
) -> None:
    configure("outgoing-service")
    configure("outgoing-service")
    levels_written = {line["level"] for line in json_lines(capsys.readouterr().out)}

    with inside_a_request():
        expected = outgoing_headers()
        call(echo.url, {})

    assert echo.last[TRACEPARENT] == [expected[TRACEPARENT]]
    assert levels_written <= {"debug", "info"}


def _causes(error: BaseException) -> Iterator[BaseException]:
    seen: BaseException | None = error
    while seen is not None:
        yield seen
        seen = seen.__cause__ or seen.__context__


@_ALL_CLIENTS
def test_a_propagator_that_raises_fails_the_request_and_nothing_is_sent(
    echo: EchoServer, call
) -> None:
    with inside_a_request():
        set_global_textmap(RaisingPropagator())
        with pytest.raises(Exception) as failure:  # noqa: B017 - each client wraps it its own way
            call(echo.url, {})

    assert any(isinstance(cause, PropagatorFailure) for cause in _causes(failure.value))
    assert echo.received == []


def test_a_botocore_request_is_signed_without_the_added_header(echo: EchoServer) -> None:
    with inside_a_request():
        CLIENTS["botocore"](echo.url, {})

    assert TRACEPARENT in echo.last
    (authorization,) = echo.last["authorization"]
    signed_headers = re.search(r"SignedHeaders=([^,]+)", authorization)
    assert signed_headers
    assert TRACEPARENT not in signed_headers.group(1).split(";")


@pytest.mark.parametrize("client_module", [httpx, httpx2])
def test_httpx_clients_with_an_in_process_transport_carry_the_trace(
    client_module: ModuleType,
) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request | httpx2.Request) -> httpx.Response | httpx2.Response:
        seen.append(request.headers.get(TRACEPARENT))
        return client_module.Response(200)

    with inside_a_request():
        expected = outgoing_headers()
        with client_module.Client(transport=client_module.MockTransport(handler)) as client:
            client.get("http://callee.invalid/")

    assert seen == [expected[TRACEPARENT]]


def test_a_host_test_through_asgi_transport_continues_the_callers_trace() -> None:
    async def call() -> dict[str, dict[str, str]]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_app()), base_url="http://callee.invalid"
        ) as client:
            return (await client.get("/trace")).json()

    with inside_a_request():
        callers_trace_id = outgoing_headers()[TRACEPARENT].split("-")[1]
        callee = asyncio.run(call())

    assert callee["headers"][TRACEPARENT].split("-")[1] == callers_trace_id


def test_a_call_made_by_a_handler_of_a_real_app_continues_the_requests_trace(
    echo: EchoServer,
) -> None:
    app = fastapi.FastAPI()
    app.add_middleware(RequestLineMiddleware)

    @app.get("/relay")
    async def relay() -> dict[str, str]:
        async with httpx.AsyncClient() as client:
            await client.get(echo.url)
        return outgoing_headers()

    answers: list[dict[str, str]] = []

    async def exercise(base_url: str) -> None:
        async with httpx.AsyncClient(base_url=base_url) as client:
            response = await client.get("/relay", headers={TRACEPARENT: INCOMING_TRACEPARENT})
            answers.append(response.json())

    serve(uvicorn.Config(app, log_config=None), exercise)

    (relayed,) = answers
    assert echo.last[TRACEPARENT] == [relayed[TRACEPARENT]]
    assert echo.last[TRACEPARENT][0].split("-")[1] == INCOMING_TRACE_ID


def test_a_process_without_httpx_and_httpx2_configures_quietly_and_still_carries_the_trace(
    echo: EchoServer,
) -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(_HIDDEN_CLIENTS_HOST), echo.url],
        capture_output=True,
        text=True,
        check=False,
    )

    assert (result.returncode, result.stderr) == (0, "")
    assert json.loads(result.stdout) == {TRACEPARENT: echo.last[TRACEPARENT][0]}
