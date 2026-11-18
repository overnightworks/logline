"""One line per HTTP request and per WebSocket session, and how a host reports the person, address
and route.

This module imports no web framework: it speaks ASGI.
"""

from dataclasses import dataclass
from enum import StrEnum

import structlog

from logline.asgi import ASGIApp, Message, Receive, Scope, Send
from logline.lifespan import run_lifespan
from logline.step import Outcome, Stopwatch
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


def _method_code(scope: Scope) -> str:
    method = scope["method"]
    return method if method in _STANDARD_METHODS else _Method.OTHER


@dataclass(slots=True)
class _RequestRecord:
    sub: str | None
    ip: str | None
    route: str | None


def _record_of(scope: Scope) -> _RequestRecord | None:
    return scope.get("state", {}).get(_STATE_KEY)


def _open_record(scope: Scope) -> _RequestRecord:
    record = _RequestRecord(sub=None, ip=_client_host(scope), route=None)
    scope.setdefault("state", {})[_STATE_KEY] = record
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


def _route_template(scope: Scope, record: _RequestRecord) -> str | None:
    if record.route is not None:
        return record.route or None
    return getattr(scope.get("route"), "path", None)


def _outcome_of_status(status: int) -> Outcome:
    return Outcome.FAILED if status >= _FIRST_SERVER_ERROR_STATUS else Outcome.OK


class RequestLineMiddleware:
    """ASGI middleware that owns three scopes and passes every other through.

    An `http` scope gets one `request.ended` line on `logline.request` and joins its trace. A
    `websocket` scope gets one `websocket.ended` line on the same logger when its session ends. A
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
        record = _open_record(scope)
        sent_status: int | None = None
        raised_outcome: Outcome | None = None
        written = False
        stopwatch = Stopwatch()

        def write_line() -> None:
            nonlocal written
            written = True
            status = sent_status or _STATUS_WHEN_NONE_SENT
            _request_logger.info(
                REQUEST_EVENT,
                outcome=raised_outcome or _outcome_of_status(status),
                method=_method_code(scope),
                route=_route_template(scope, record),
                status=status,
                duration_ms=stopwatch.elapsed_ms(),
                sub=record.sub,
                ip=record.ip,
            )

        async def send_noting_status(message: Message) -> None:
            nonlocal sent_status
            if message["type"] == "http.response.start":
                sent_status = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                write_line()

        with request_trace(scope["headers"]):
            try:
                await self._app(scope, receive, send_noting_status)
            except Exception:
                raised_outcome = Outcome.FAILED
                raise
            except BaseException:
                raised_outcome = Outcome.CANCELLED
                raise
            finally:
                if not written:
                    write_line()

    async def _write_session_line(self, scope: Scope, receive: Receive, send: Send) -> None:
        record = _open_record(scope)
        stopwatch = Stopwatch()

        def write_line(end: SessionEnd) -> None:
            _request_logger.info(
                _SESSION_EVENT,
                outcome=end.outcome,
                route=_route_template(scope, record),
                duration_ms=stopwatch.elapsed_ms(),
                sub=record.sub,
                ip=record.ip,
                close_code=end.close_code,
                **({} if end.status is None else {"status": end.status}),
            )

        await run_websocket(self._app, scope, receive, send, write_line)
