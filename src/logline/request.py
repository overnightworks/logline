"""One request line per HTTP request, and how a host reports the person and address.

This module imports no web framework: it speaks ASGI.
"""

import time
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass
from typing import Any

import structlog

from logline.trace import request_trace

__all__ = [
    "REQUEST_EVENT",
    "REQUEST_LOGGER_NAME",
    "RequestLineMiddleware",
    "set_client_ip",
    "set_sub",
]

type Scope = MutableMapping[str, Any]
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

REQUEST_LOGGER_NAME = "logline.request"
REQUEST_EVENT = "request"
_STATE_KEY = "logline.request"
_STATUS_WHEN_NONE_SENT = 500

_request_logger = structlog.get_logger(REQUEST_LOGGER_NAME)


@dataclass(slots=True)
class _RequestRecord:
    sub: str | None
    ip: str | None


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


def _client_host(scope: Scope) -> str | None:
    client = scope.get("client")
    return client[0] if client else None


def _mount_template(routes: Iterable[Any], mount_path: str) -> str:
    """The path templates of the nested mounts whose matched paths add up to `mount_path`.

    Empty where no mount in `routes` matches it. A mount is a route that has a `path_regex` with a
    `path` group for what it hands on, and routes of its own.
    """
    if not mount_path:
        return ""
    for route in routes:
        path_regex = getattr(route, "path_regex", None)
        inner_routes = getattr(route, "routes", None)
        if path_regex is None or inner_routes is None:
            continue
        match = path_regex.match(f"{mount_path}/")
        if match is not None:
            handed_on = mount_path[match.start("path") - 1 :]
            return f"{route.path}{_mount_template(inner_routes, handed_on)}"
    return ""


def _route_template(scope: Scope) -> str | None:
    """The matched mounts' templates joined with the matched route's; `None` if neither matched.

    Starlette leaves what the mounts matched, the request's own path segments, in `root_path` past
    the `app_root_path` the server set, so the mounts' templates come from the outermost `router`;
    the matched route's template is on `route`. A mount that cannot be found adds nothing.
    """
    app_root_path = scope.get("app_root_path")
    mount_path = "" if app_root_path is None else scope["root_path"].removeprefix(app_root_path)
    mount = _mount_template(getattr(scope.get("router"), "routes", ()), mount_path)
    route = getattr(scope.get("route"), "path", "")
    return f"{mount}{route}" or None


class RequestLineMiddleware:
    """ASGI middleware: writes one `logline.request` line per `http` scope and joins its trace."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        record = _RequestRecord(sub=None, ip=_client_host(scope))
        scope.setdefault("state", {})[_STATE_KEY] = record
        sent_status: int | None = None
        written = False
        started = time.perf_counter()

        def write_line() -> None:
            nonlocal written
            written = True
            _request_logger.info(
                REQUEST_EVENT,
                method=scope["method"],
                route=_route_template(scope),
                status=sent_status or _STATUS_WHEN_NONE_SENT,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
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
            finally:
                if not written:
                    write_line()
