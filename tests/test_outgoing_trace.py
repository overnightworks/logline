import contextlib
import contextvars
import http.client
import json
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import InterpreterPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
from urllib.parse import urlsplit

import fastapi
import httpx
import httpx2
import pytest
import urllib3
import uvicorn
import websockets.asyncio.client
import websockets.client
from opentelemetry.propagate import set_global_textmap

from logline import RequestLineMiddleware, configure, outgoing_headers, unit_trace
from logline.trace import current_trace_id, current_traceparent, request_trace
from tests.support.apps import WEBSOCKET_TRACE_PATH, build_app
from tests.support.incoming_trace import (
    INCOMING_TRACE_ID,
    INCOMING_TRACEPARENT,
    TRACEPARENT_PATTERN,
)
from tests.support.lines import json_lines, session_lines
from tests.support.outgoing_clients import (
    A_CALLERS_TRACE_ID,
    A_CALLERS_TRACEPARENT,
    CLIENTS,
    SCOPES,
    TRACEPARENT,
    TRACESTATE,
    TRANSFERS,
    EchoServer,
    inside_a_request,
    inside_a_step,
    inside_a_turn,
)
from tests.support.propagators import PropagatorFailure, RaisingPropagator
from tests.support.serving import serve

_HIDDEN_CLIENTS_HOST = Path(__file__).parent / "support" / "hidden_clients_host.py"
_INHERITED_CONTEXT_HOST = Path(__file__).parent / "support" / "inherited_context_host.py"
_ALL_CLIENTS = pytest.mark.parametrize("call", CLIENTS.values(), ids=CLIENTS.keys())
_A_WORKER_VARIABLE = contextvars.ContextVar("a_worker_variable", default="not set")


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


def test_with_otel_propagators_none_a_work_item_still_sees_the_submitters_trace(
    host_with_otel_propagators_none: None,
) -> None:
    configure("outgoing-service")

    with ThreadPoolExecutor(max_workers=1) as pool, inside_a_request():
        trace_of_the_request = current_trace_id()
        seen_by_the_item = pool.submit(current_trace_id).result()

    assert trace_of_the_request is not None
    assert seen_by_the_item == trace_of_the_request


@pytest.mark.parametrize(
    ("client_class", "methods", "call"),
    [
        pytest.param(
            http.client.HTTPConnection,
            ("putrequest", "putheader", "endheaders"),
            CLIENTS["http.client"],
            id="http.client",
        ),
        pytest.param(httpx.Client, ("send",), CLIENTS["httpx"], id="httpx"),
        pytest.param(httpx.AsyncClient, ("send",), CLIENTS["httpx-async"], id="httpx-async"),
        pytest.param(httpx2.Client, ("send",), CLIENTS["httpx2"], id="httpx2"),
        pytest.param(httpx2.AsyncClient, ("send",), CLIENTS["httpx2-async"], id="httpx2-async"),
        pytest.param(
            websockets.client.ClientProtocol,
            ("send_request",),
            CLIENTS["websockets"],
            id="websockets",
        ),
        pytest.param(
            ThreadPoolExecutor, ("submit",), CLIENTS["thread-pool-submit"], id="thread-pool-submit"
        ),
    ],
)
def test_a_second_configure_adds_no_carrier_keeps_a_hosts_wrapper_and_writes_no_line_above_info(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    echo: EchoServer,
    client_class: type,
    methods: tuple[str, ...],
    call,
) -> None:
    last_method = methods[-1]
    inner = getattr(client_class, last_method)

    def hosts_wrapper(*args: object, **kwargs: object) -> object:
        return inner(*args, **kwargs)

    monkeypatch.setattr(client_class, last_method, hosts_wrapper)
    before = {method: getattr(client_class, method) for method in methods}

    configure("outgoing-service")
    levels_written = {line["level"] for line in json_lines(capsys.readouterr().out)}

    assert {method: getattr(client_class, method) for method in methods} == before
    assert levels_written <= {"debug", "info"}
    with inside_a_request():
        expected = outgoing_headers()
        call(echo.url, {})
    assert echo.last[TRACEPARENT] == [expected[TRACEPARENT]]


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


def _assert_signed_without_the_trace(headers: dict[str, list[str]]) -> None:
    assert TRACEPARENT in headers
    (authorization,) = headers["authorization"]
    signed_headers = re.search(r"SignedHeaders=([^,]+)", authorization)
    assert signed_headers
    assert TRACEPARENT not in signed_headers.group(1).split(";")


def test_a_botocore_request_is_signed_without_the_added_header(echo: EchoServer) -> None:
    with inside_a_request():
        CLIENTS["botocore"](echo.url, {})

    _assert_signed_without_the_trace(echo.last)


@pytest.mark.parametrize("client_module", [httpx, httpx2], ids=["httpx", "httpx2"])
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


def test_a_websockets_client_opened_inside_a_step_reaches_a_real_app_in_the_steps_span(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("outgoing-service")
    step_spans: list[str] = []

    async def exercise(base_url: str) -> None:
        with inside_a_step():
            match = TRACEPARENT_PATTERN.match(str(current_traceparent()))
            assert match
            step_spans.append(match["span_id"])
            url = base_url.replace("http", "ws") + WEBSOCKET_TRACE_PATH
            async with websockets.asyncio.client.connect(url) as websocket:
                await websocket.wait_closed()

    serve(uvicorn.Config(build_app(), log_config=None), exercise)

    (session,) = session_lines(json_lines(capsys.readouterr().out))
    assert (session["trace_id"], session["parent_span_id"]) == (INCOMING_TRACE_ID, *step_spans)


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


@pytest.mark.parametrize(("transfer", "methods"), TRANSFERS.values(), ids=TRANSFERS.keys())
def test_a_managed_transfer_of_boto3_inside_a_trace_carries_it_on_every_request(
    echo: EchoServer, transfer: Callable[[str], None], methods: set[str]
) -> None:
    with inside_a_request():
        expected = outgoing_headers()
        transfer(echo.url)

    assert {request.method for request in echo.received} == methods
    assert all(request.headers[TRACEPARENT] == [expected[TRACEPARENT]] for request in echo.received)
    for request in echo.received:
        _assert_signed_without_the_trace(request.headers)


def _trace_id_of(headers: dict[str, list[str]]) -> str | None:
    return headers[TRACEPARENT][0].split("-")[1] if TRACEPARENT in headers else None


def _item_of_trace_ids(echo: EchoServer) -> dict[str, str | None]:
    return {
        request.headers["x-item"][0]: _trace_id_of(request.headers) for request in echo.received
    }


def test_items_on_several_workers_each_carry_the_trace_they_were_submitted_in(
    echo: EchoServer,
) -> None:
    workers = 4
    all_workers_busy = threading.Barrier(workers, timeout=10)

    def send(item: str) -> None:
        all_workers_busy.wait()
        CLIENTS["http.client"](echo.url, {"x-item": item})

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        for name, traceparent in (
            ("first", INCOMING_TRACEPARENT),
            ("second", A_CALLERS_TRACEPARENT),
        ):
            with unit_trace(traceparent):
                futures += [pool.submit(send, f"{name}-{number}") for number in range(2)]
        for future in futures:
            future.result()

    assert _item_of_trace_ids(echo) == {
        "first-0": INCOMING_TRACE_ID,
        "first-1": INCOMING_TRACE_ID,
        "second-0": A_CALLERS_TRACE_ID,
        "second-1": A_CALLERS_TRACE_ID,
    }


def test_the_items_of_a_one_worker_pool_carry_the_trace_of_their_own_submit_or_none(
    echo: EchoServer,
) -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        for scope in (
            unit_trace(INCOMING_TRACEPARENT),
            unit_trace(A_CALLERS_TRACEPARENT),
            contextlib.nullcontext(),
        ):
            with scope:
                pool.submit(CLIENTS["http.client"], echo.url, {}).result()

    assert [_trace_id_of(request.headers) for request in echo.received] == [
        INCOMING_TRACE_ID,
        A_CALLERS_TRACE_ID,
        None,
    ]


def _leaves_a_trace_attached() -> None:
    with request_trace([(TRACEPARENT, A_CALLERS_TRACEPARENT)]):
        raise RuntimeError


def _raises() -> None:
    raise RuntimeError


@pytest.mark.parametrize(
    "failing_item",
    [_leaves_a_trace_attached, _raises],
    ids=["leaves-a-trace-attached", "raises"],
)
def test_an_item_that_fails_passes_no_trace_to_the_next_item_on_its_worker(
    echo: EchoServer, failing_item: Callable[[], None]
) -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        with inside_a_request():
            assert isinstance(pool.submit(failing_item).exception(), RuntimeError)
        pool.submit(CLIENTS["http.client"], echo.url, {}).result()

    assert TRACEPARENT not in echo.last


def test_a_context_variable_an_item_sets_persists_to_the_next_item_on_its_worker() -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        with inside_a_request():
            pool.submit(_A_WORKER_VARIABLE.set, "set by an item").result()
        assert pool.submit(_A_WORKER_VARIABLE.get).result() == "set by an item"
        token = _A_WORKER_VARIABLE.set("set by the submitter")
        try:
            assert pool.submit(_A_WORKER_VARIABLE.get).result() == "set by an item"
        finally:
            _A_WORKER_VARIABLE.reset(token)


def test_the_submitters_trace_is_detached_when_an_item_ends() -> None:
    item_running = threading.Event()
    seen_after_the_item: list[str | None] = []

    with ThreadPoolExecutor(max_workers=1) as pool:
        with unit_trace(A_CALLERS_TRACEPARENT):
            future = pool.submit(item_running.wait, 10)
            future.add_done_callback(lambda _: seen_after_the_item.append(current_trace_id()))
        item_running.set()
        future.result()

    assert seen_after_the_item == [None]


def test_an_interpreter_pool_still_runs_its_work() -> None:
    with InterpreterPoolExecutor(max_workers=1) as pool, inside_a_request():
        assert pool.submit(pow, 2, 3).result() == 8


def test_with_thread_inherit_context_a_worker_started_in_a_trace_carries_no_stale_trace(
    echo: EchoServer,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-X",
            "thread_inherit_context=1",
            str(_INHERITED_CONTEXT_HOST),
            echo.url,
            INCOMING_TRACEPARENT,
            A_CALLERS_TRACEPARENT,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert (result.returncode, result.stderr) == (0, "")
    assert [_trace_id_of(request.headers) for request in echo.received] == [
        None,
        A_CALLERS_TRACE_ID,
    ]
