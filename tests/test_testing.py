import contextlib
import http.client
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx
import httpx2
import pytest
import structlog
from opentelemetry.propagate import get_global_textmap, set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from logline import configure
from logline.testing import configured
from logline.trace import current_trace_id
from tests.support.incoming_trace import INCOMING_TRACE_ID
from tests.support.lines import json_lines
from tests.support.loggers import logger_settings
from tests.support.outgoing_clients import CLIENTS, TRACEPARENT, EchoServer, inside_a_request


@dataclass(frozen=True)
class _Seen:
    """What a host can observe of the process's logging."""

    loggers: dict[str, tuple[int, bool, tuple[object, ...], tuple[object, ...]]]
    structlog_is_configured: bool
    structlog_processors: list[object]


def _seen() -> _Seen:
    return _Seen(
        loggers=logger_settings(),
        structlog_is_configured=structlog.is_configured(),
        structlog_processors=list(structlog.get_config()["processors"]),
    )


def _leave_logging_in_a_host_state() -> None:
    uvicorn_error = logging.getLogger("uvicorn.error")
    uvicorn_error.addHandler(logging.NullHandler())
    uvicorn_error.addFilter(lambda record: True)
    uvicorn_error.setLevel(logging.ERROR)
    uvicorn_error.propagate = False
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger().setLevel(logging.DEBUG)


def test_lines_inside_the_block_are_json(capsys: pytest.CaptureFixture[str]) -> None:
    with configured("inside"):
        logging.getLogger("some_library").warning("a line")

    (line,) = json_lines(capsys.readouterr().out)
    assert (line["service"], line["event"]) == ("inside", "a line")


@pytest.mark.parametrize("structlog_configured_before", [False, True])
def test_leaving_the_block_restores_the_logging_the_host_had(
    structlog_configured_before: bool,
) -> None:
    _leave_logging_in_a_host_state()
    if structlog_configured_before:
        structlog.configure(processors=[structlog.processors.add_log_level])
    before = _seen()

    with configured("restoring"):
        assert _seen() != before

    assert _seen() == before


def test_leaving_the_block_restores_the_logger_class_the_host_had() -> None:
    class HostLogger(logging.Logger):
        pass

    original_class = logging.getLoggerClass()
    logging.setLoggerClass(HostLogger)
    try:
        with configured("logger-class"):
            pass

        assert logging.getLoggerClass() is HostLogger
    finally:
        logging.setLoggerClass(original_class)


def test_a_failing_block_still_restores_the_logging() -> None:
    before = _seen()

    with pytest.raises(ZeroDivisionError), configured("failing"):
        raise ZeroDivisionError

    assert _seen() == before


def test_blocks_in_a_row_each_restore_and_no_handler_accumulates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _seen()

    with configured("first"):
        logging.getLogger("some_library").warning("in the first")
    with configured("second"):
        logging.getLogger("some_library").warning("in the second")

    assert _seen() == before
    assert [line["event"] for line in json_lines(capsys.readouterr().out)] == [
        "in the first",
        "in the second",
    ]


def test_a_block_inside_a_process_configured_earlier_restores_that_configuration(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("outer")
    with configured("inner"):
        logging.getLogger("some_library").warning("inner line")
    logging.getLogger("some_library").warning("outer line")

    services = {line["event"]: line["service"] for line in json_lines(capsys.readouterr().out)}
    assert services == {"inner line": "inner", "outer line": "outer"}


@pytest.mark.parametrize("block_raises", [False, True], ids=["returns", "raises"])
def test_leaving_the_block_restores_the_global_propagator_the_host_had(block_raises: bool) -> None:
    host_propagator = CompositePropagator([])
    set_global_textmap(host_propagator)

    with contextlib.suppress(RuntimeError), configured("propagator"):
        assert isinstance(get_global_textmap(), TraceContextTextMapPropagator)
        if block_raises:
            raise RuntimeError

    assert get_global_textmap() is host_propagator


@pytest.mark.parametrize("call", CLIENTS.values(), ids=CLIENTS.keys())
def test_leaving_the_block_stops_the_trace_reaching_outgoing_calls(
    echo: EchoServer, call: Callable[[str, dict[str, str]], None]
) -> None:
    with configured("outgoing"), inside_a_request():
        call(echo.url, {})
    carried_inside = TRACEPARENT in echo.last

    with inside_a_request():
        call(echo.url, {})

    assert (carried_inside, TRACEPARENT in echo.last) == (True, False)


def test_leaving_the_block_stops_a_thread_pool_item_seeing_the_submitters_trace() -> None:
    with configured("pool"), ThreadPoolExecutor(max_workers=1) as pool, inside_a_request():
        seen_inside = pool.submit(current_trace_id).result()

    with ThreadPoolExecutor(max_workers=1) as pool, inside_a_request():
        seen_after = pool.submit(current_trace_id).result()

    assert (seen_inside, seen_after) == (INCOMING_TRACE_ID, None)


@pytest.mark.parametrize(
    ("client_class", "method", "call"),
    [
        pytest.param(
            http.client.HTTPConnection, "endheaders", CLIENTS["http.client"], id="http.client"
        ),
        pytest.param(httpx.Client, "send", CLIENTS["httpx"], id="httpx"),
        pytest.param(httpx.AsyncClient, "send", CLIENTS["httpx-async"], id="httpx-async"),
        pytest.param(httpx2.Client, "send", CLIENTS["httpx2"], id="httpx2"),
        pytest.param(httpx2.AsyncClient, "send", CLIENTS["httpx2-async"], id="httpx2-async"),
        pytest.param(
            ThreadPoolExecutor, "submit", CLIENTS["thread-pool-submit"], id="thread-pool-submit"
        ),
    ],
)
def test_a_wrapper_put_on_a_patched_method_after_configure_survives_the_block(
    monkeypatch: pytest.MonkeyPatch,
    echo: EchoServer,
    client_class: type,
    method: str,
    call: Callable[[str, dict[str, str]], None],
) -> None:
    wrapped_calls: list[object] = []
    with configured("wrapped"):
        patched = getattr(client_class, method)

        def wrapper(*args: object, **kwargs: object) -> object:
            wrapped_calls.append(args[0])
            return patched(*args, **kwargs)

        monkeypatch.setattr(client_class, method, wrapper)

    with inside_a_request():
        call(echo.url, {})

    assert getattr(client_class, method) is wrapper
    assert (len(wrapped_calls), TRACEPARENT in echo.last) == (1, False)
