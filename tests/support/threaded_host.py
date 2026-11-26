import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from logline import RequestLine, StepTimer, configure, unit_trace
from tests.support.apps import HOST_CLIENT_IP, HOST_ROUTE, SUB
from tests.support.lines import Line, json_lines
from tests.support.outgoing_clients import A_CALLERS_TRACEPARENT
from tests.support.serving import Call
from tests.support.serving_in_a_thread import serving_in_a_thread

HOST_LOGGER = "tests.threaded_host"
HANDLED_EVENT = "handler.logged"
HANDLE_ERROR_EVENT = "request_handling.failed"

OK_PATH = "/ok"
REFUSED_PATH = "/refused"
UNAVAILABLE_PATH = "/unavailable"
RAISES_PATH = "/raises"
CANCELLED_PATH = "/cancelled"
NO_STATUS_PATH = "/no-status"
IDENTITY_PATH = "/identity"
LOGGING_PATH = "/logging"
ENDED_EARLY_PATH = "/ended-early"
STEP_PATH = "/step"
ENDED_IN_ANOTHER_TRACE_PATH = "/ended-in-another-trace"
STEP_ENTERED_OUTSIDE_A_TRACE_PATH = "/step-entered-outside-a-trace"

_STATUS_REFUSED = 404
_STATUS_UNAVAILABLE = 503


class HostCancelled(BaseException):
    """What a host's handler raises when it is cancelled from outside, as a `BaseException` is."""


def _reply(handler: BaseHTTPRequestHandler, status: int) -> None:
    handler.send_response(status)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _answer(handler: BaseHTTPRequestHandler, line: RequestLine, status: int) -> None:
    line.set_status(status)
    _reply(handler, status)


def _ok(handler: BaseHTTPRequestHandler, line: RequestLine) -> None:
    _answer(handler, line, 200)


def _refused(handler: BaseHTTPRequestHandler, line: RequestLine) -> None:
    _answer(handler, line, _STATUS_REFUSED)


def _unavailable(handler: BaseHTTPRequestHandler, line: RequestLine) -> None:
    _answer(handler, line, _STATUS_UNAVAILABLE)


def _raises(_handler: BaseHTTPRequestHandler, _line: RequestLine) -> None:
    raise RuntimeError("the handler failed")


def _cancelled(_handler: BaseHTTPRequestHandler, _line: RequestLine) -> None:
    raise HostCancelled


def _no_status(handler: BaseHTTPRequestHandler, _line: RequestLine) -> None:
    _reply(handler, 200)


def _identity(handler: BaseHTTPRequestHandler, line: RequestLine) -> None:
    line.set_sub(SUB)
    line.set_client_ip(HOST_CLIENT_IP)
    line.set_route(HOST_ROUTE)
    _answer(handler, line, 200)


def _logging(handler: BaseHTTPRequestHandler, line: RequestLine) -> None:
    logging.getLogger(HOST_LOGGER).info(HANDLED_EVENT)
    _answer(handler, line, 200)


def _ended_early(handler: BaseHTTPRequestHandler, line: RequestLine) -> None:
    line.set_status(200)
    line.end()
    _reply(handler, 200)


def _step(handler: BaseHTTPRequestHandler, line: RequestLine) -> None:
    with StepTimer(HANDLED_EVENT, logging.getLogger(HOST_LOGGER)):
        pass
    _answer(handler, line, 200)


def _ended_in_another_trace(handler: BaseHTTPRequestHandler, line: RequestLine) -> None:
    line.set_status(200)
    with unit_trace(A_CALLERS_TRACEPARENT):
        line.end()
    _reply(handler, 200)


def _step_entered_outside_a_trace(handler: BaseHTTPRequestHandler, line: RequestLine) -> None:
    def run() -> None:
        # A new thread starts with an empty context: this step begins outside any trace.
        timer = StepTimer(HANDLED_EVENT, logging.getLogger(HOST_LOGGER))
        timer.__enter__()
        with unit_trace(A_CALLERS_TRACEPARENT):
            timer.__exit__(None, None, None)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    _answer(handler, line, 200)


_ROUTES: dict[str, Callable[[BaseHTTPRequestHandler, RequestLine], None]] = {
    OK_PATH: _ok,
    REFUSED_PATH: _refused,
    UNAVAILABLE_PATH: _unavailable,
    RAISES_PATH: _raises,
    CANCELLED_PATH: _cancelled,
    NO_STATUS_PATH: _no_status,
    IDENTITY_PATH: _identity,
    LOGGING_PATH: _logging,
    ENDED_EARLY_PATH: _ended_early,
    STEP_PATH: _step,
    ENDED_IN_ANOTHER_TRACE_PATH: _ended_in_another_trace,
    STEP_ENTERED_OUTSIDE_A_TRACE_PATH: _step_entered_outside_a_trace,
}


class _Handler(BaseHTTPRequestHandler):
    """A host's handler: the whole request runs inside its `RequestLine`."""

    def _serve(self) -> None:
        with RequestLine(self.command, self.headers.items(), ip=self.client_address[0]) as line:
            _ROUTES[self.path](self, line)

    do_GET = _serve
    do_DELETE = _serve
    do_BREW = _serve

    def log_message(self, format: str, *args: Any) -> None:
        """The standard library's access line carries the raw path; the host drops it."""


class _Host(ThreadingHTTPServer):
    # Not daemon threads: `server_close` joins them, so every line is written when it returns.
    daemon_threads = False

    def handle_error(self, request: Any, client_address: Any) -> None:
        """The host's duty: the layer where the exception stops writes its one `.failed` line."""
        logging.getLogger(HOST_LOGGER).error(HANDLE_ERROR_EVENT)


def serve_threaded_requests(
    capsys: pytest.CaptureFixture[str], calls: list[Call]
) -> tuple[str, list[Line]]:
    """Serve the host with a real `ThreadingHTTPServer` in a thread, make `calls`, return stdout."""
    configure("request-service")
    server = _Host(("127.0.0.1", 0), _Handler)
    with (
        serving_in_a_thread(server),
        httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as client,
    ):
        for call in calls:
            try:
                client.request(call.method, call.path, headers=call.headers)
            except httpx.RemoteProtocolError:
                pass  # a request whose handler raised ends without an answer
    output = capsys.readouterr().out
    return output, json_lines(output)
