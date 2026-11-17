"""One request line per HTTP request, and how a host reports the person, address and route.

This module imports no web framework: it speaks ASGI.
"""

from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog

from logline.step import Outcome, Stopwatch
from logline.trace import request_trace

__all__ = [
    "REQUEST_EVENT",
    "REQUEST_LOGGER_NAME",
    "RequestLineMiddleware",
    "set_client_ip",
    "set_route",
    "set_sub",
]

type Scope = MutableMapping[str, Any]
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

REQUEST_LOGGER_NAME = "logline.request"
REQUEST_EVENT = "request.ended"
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


def set_sub(scope: Scope, sub: str | None) -> None:
    """Report the person's `sub` for this request's line; no effect outside an HTTP request."""
    record = _record_of(scope)
    if record is not None:
        record.sub = sub


def set_client_ip(scope: Scope, ip: str | None) -> None:
    """Report the client address for this request's line; no effect outside an HTTP request."""
    record = _record_of(scope)
    if record is not None:
        record.ip = ip


def set_route(scope: Scope, template: str) -> None:
    """Report the route template for this request's line; no effect outside an HTTP request.

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
    """ASGI middleware: writes one `logline.request` line per `http` scope and joins its trace."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        record = _RequestRecord(sub=None, ip=_client_host(scope), route=None)
        scope.setdefault("state", {})[_STATE_KEY] = record
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
