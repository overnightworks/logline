"""One line per HTTP request and per WebSocket session, and how a host reports the person, address
and route.

This module imports no web framework: it speaks ASGI, and `RequestLine` serves a host that does not.
"""

from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType

import structlog

from logline.asgi import ASGIApp, Message, Receive, Scope, Send
from logline.lifespan import run_lifespan
from logline.step import Outcome, Stopwatch, outcome_of_raised
from logline.trace import request_trace
from logline.websocket import SessionEnd, run_websocket

__all__: list[str] = []

REQUEST_LOGGER_NAME = "logline.request"
REQUEST_EVENT = "request.ended"
_SESSION_EVENT = "websocket.ended"
_STATE_KEY = "logline.request"
_STATUS_WHEN_NONE_SENT = 500
_FIRST_SERVER_ERROR_STATUS = 500

_request_logger = structlog.get_logger(REQUEST_LOGGER_NAME)


class _Method(StrEnum):
    """The standard HTTP methods; any other token is `_OTHER`, which bounds the field's values."""

    CONNECT = "CONNECT"
    DELETE = "DELETE"
    GET = "GET"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"
    PATCH = "PATCH"
    POST = "POST"
    PUT = "PUT"
    TRACE = "TRACE"
    OTHER = "_OTHER"


_STANDARD_METHODS = frozenset(_Method) - {_Method.OTHER}


def _method_code(method: str) -> str:
    return method if method in _STANDARD_METHODS else _Method.OTHER


@dataclass(slots=True)
class _RequestRecord:
    sub: str | None
    ip: str | None
    route: str | None


def _record_of(scope: Scope) -> _RequestRecord | None:
    return scope.get("state", {}).get(_STATE_KEY)


def _share_record(scope: Scope, record: _RequestRecord) -> None:
    scope.setdefault("state", {})[_STATE_KEY] = record


def _open_record(scope: Scope) -> _RequestRecord:
    record = _RequestRecord(sub=None, ip=_client_host(scope), route=None)
    _share_record(scope, record)
    return record


def set_sub(scope: Scope, sub: str | None) -> None:
    """Report the person's `sub` for this request's or session's line; no effect elsewhere."""
    record = _record_of(scope)
    if record is not None:
        record.sub = sub


def set_client_ip(scope: Scope, ip: str | None) -> None:
    """Report the client address for this request's or session's line; no effect elsewhere."""
    record = _record_of(scope)
    if record is not None:
        record.ip = ip


def set_route(scope: Scope, template: str) -> None:
    """Report the route template for this request's or session's line; no effect elsewhere.

    The template is a literal pattern such as `/static`, never the raw path or a request value.
    An empty template logs `null` and does not fall back to the framework's route.
    """
    record = _record_of(scope)
    if record is not None:
        record.route = template


def _client_host(scope: Scope) -> str | None:
    client = scope.get("client")
    return client[0] if client else None


def _note_framework_route(scope: Scope, record: _RequestRecord) -> None:
    if record.route is None:
        record.route = getattr(scope.get("route"), "path", None)


def _outcome_of_status(status: int) -> Outcome:
    return Outcome.FAILED if status >= _FIRST_SERVER_ERROR_STATUS else Outcome.OK


class RequestLine:
    """Write one `request.ended` line for a request a host serves without ASGI.

    Use it as a block around the whole request: `with RequestLine(method, headers, ip=address) as
    line:`. The block runs in the request's trace, the incoming one from `headers` or a new one, so
    every line logged inside carries its `trace_id`. The stopwatch starts when the object is
    constructed, and the object serves one request.

    - `method` is the request's method; any token outside the standard set reads `_OTHER`.
    - `headers` are the request's (name, value) pairs of text, read case-insensitively; only
      `traceparent` and `tracestate` are used.
    - `ip` is the client address, `None` when the host has none to report.

    `set_status` sets the status the host sends, where it sends it, not earlier. `set_sub`,
    `set_client_ip` and `set_route` set the other fields at any point of the block; `set_route`
    takes a literal route template, never the path or a request value. `end` writes the line
    now, for a host whose response ends before the block does. Leaving the block writes it once if
    `end` did not: `ok` below status 500 and `failed` from it, `failed` after an `Exception`
    left the block, `cancelled` after any other `BaseException`, and status 500 when none was set.
    An exception that leaves the block stays attached to the trace, so the host's own failure line
    belongs to it, so a host that catches the exception outside the block and keeps the
    connection stacks the next request's trace on it; the standard library closes the connection.
    """

    def __init__(
        self, method: str, headers: Iterable[tuple[str, str]], *, ip: str | None = None
    ) -> None:
        self._open(method, request_trace(headers), ip)

    @classmethod
    def _of_scope(cls, scope: Scope) -> RequestLine:
        """The middleware's line: the scope's byte header pairs are no public parameter."""
        line = cls.__new__(cls)
        line._open(scope["method"], request_trace(scope["headers"]), _client_host(scope))
        return line

    def _open(self, method: str, trace: AbstractContextManager[None], ip: str | None) -> None:
        self._method = _method_code(method)
        self._record = _RequestRecord(sub=None, ip=ip, route=None)
        self._trace = trace
        self._status: int | None = None
        self._written = False
        self._stopwatch = Stopwatch()

    def __enter__(self) -> RequestLine:
        self._trace.__enter__()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._write(_outcome_of_exception(exception_type))
        self._trace.__exit__(exception_type, exception, traceback)

    def set_status(self, status: int) -> None:
        """Report the status the host sends, where it sends it."""
        self._status = status

    def set_sub(self, sub: str | None) -> None:
        """Report the person's `sub` for this request's line."""
        self._record.sub = sub

    def set_client_ip(self, ip: str | None) -> None:
        """Report the client address for this request's line."""
        self._record.ip = ip

    def set_route(self, template: str) -> None:
        """Report the route template for this request's line; an empty one logs `null`."""
        self._record.route = template

    def end(self) -> None:
        """Write the line now; a later `end` or the end of the block writes nothing more."""
        self._write(None)

    def _write(self, raised_outcome: Outcome | None) -> None:
        if self._written:
            return
        self._written = True
        status = self._status or _STATUS_WHEN_NONE_SENT
        _request_logger.info(
            REQUEST_EVENT,
            outcome=raised_outcome or _outcome_of_status(status),
            method=self._method,
            route=self._record.route or None,
            status=status,
            duration_ms=self._stopwatch.elapsed_ms(),
            sub=self._record.sub,
            ip=self._record.ip,
        )


def _outcome_of_exception(exception_type: type[BaseException] | None) -> Outcome | None:
    return None if exception_type is None else outcome_of_raised(exception_type)


class RequestLineMiddleware:
    """ASGI middleware that owns three scopes and passes every other through.

    An `http` scope gets one `request.ended` line on `logline.request` and joins its trace. A
    `websocket` scope gets one `websocket.ended` line on the same logger when its session ends, and
    its session runs in a trace of its own, the upgrade's or a new one. A
    `lifespan` scope gets the app startup and shutdown lines on `logline.lifespan`, under any ASGI
    server.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        match scope["type"]:
            case "http":
                await self._write_request_line(scope, receive, send)
            case "websocket":
                await self._write_session_line(scope, receive, send)
            case "lifespan":
                await run_lifespan(self._app, scope, receive, send)
            case _:
                await self._app(scope, receive, send)

    async def _write_request_line(self, scope: Scope, receive: Receive, send: Send) -> None:
        line = RequestLine._of_scope(scope)
        record = line._record  # the per-scope record is RequestLine's own, shared with the setters
        _share_record(scope, record)

        async def send_noting_status(message: Message) -> None:
            if message["type"] == "http.response.start":
                line.set_status(message["status"])
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                _note_framework_route(scope, record)
                line.end()

        with line:
            try:
                await self._app(scope, receive, send_noting_status)
            finally:
                _note_framework_route(scope, record)

    async def _write_session_line(self, scope: Scope, receive: Receive, send: Send) -> None:
        record = _open_record(scope)
        stopwatch = Stopwatch()

        def write_line(end: SessionEnd) -> None:
            _note_framework_route(scope, record)
            _request_logger.info(
                _SESSION_EVENT,
                outcome=end.outcome,
                route=record.route or None,
                duration_ms=stopwatch.elapsed_ms(),
                sub=record.sub,
                ip=record.ip,
                close_code=end.close_code,
                **({} if end.status is None else {"status": end.status}),
            )

        with request_trace(scope["headers"]):
            await run_websocket(self._app, scope, receive, send, write_line)
