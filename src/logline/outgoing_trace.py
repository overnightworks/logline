"""Carries the current trace out of the caller's hand, whatever call site: on every outgoing HTTP
request of the process, a WebSocket opening handshake included, and into every work item a thread
pool runs.

Four carriers do it, each installed once and never removed; the state this module owns, on or off,
decides whether they add anything:

- a hook on `http.client.HTTPConnection`, which `urllib.request`, urllib3, requests and botocore go
  through; a CONNECT tunnel and urllib3's optional HTTP/2 do not pass it;
- a hook on the public `send` of httpx's and httpx2's `Client` and `AsyncClient`, where the host
  installed them, which runs before every transport, custom and in-process ones included;
- a hook on the public `send_request` of websockets' `ClientProtocol`, where the host installed
  it, which the asyncio and the sync client send their opening handshake through after the
  caller's headers are merged; `websockets.legacy` does not pass it;
- a hook on `concurrent.futures.ThreadPoolExecutor.submit`, which `Executor.map`, asyncio's
  `run_in_executor` and boto3's managed transfers go through: each work item runs with the
  OpenTelemetry context its submitter had, attached and detached around the item, so the item sees
  the submitter's trace whichever worker runs it and whatever an earlier item or the worker's start
  left attached. No other context variable is carried. An executor class that overrides
  `prepare_context`, such as `InterpreterPoolExecutor`, is left alone: its items cannot be wrapped.

A request that already carries `traceparent` gets no propagator header at all, so a caller's
`traceparent` is never paired with the current context's `tracestate`. A propagator that raises
fails the request.

This is the one module that imports an HTTP or WebSocket client; `.importlinter` allows no other.
"""

import functools
import http.client
import importlib.util
from collections.abc import Callable, MutableMapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Protocol
from weakref import WeakKeyDictionary

from opentelemetry.context import get_current

from logline.trace import TRACEPARENT_HEADER, attached, header_text, outgoing_headers

__all__: list[str] = []


@dataclass
class _Carrying:
    enabled: bool = False
    installed: set[str] = field(default_factory=set)


_carrying = _Carrying()
# The header names each connection's current request has put. A connection outlives its request
# when a pool keeps it alive, so `putrequest` starts the record again.
_header_names: WeakKeyDictionary[http.client.HTTPConnection, set[str]] = WeakKeyDictionary()


class _Request(Protocol):
    @property
    def headers(self) -> MutableMapping[str, str]: ...


def _added_headers(request_has_traceparent: bool) -> dict[str, str]:
    if not _carrying.enabled or request_has_traceparent:
        return {}
    return outgoing_headers()


def _install_http_client() -> None:
    putrequest = http.client.HTTPConnection.putrequest
    putheader = http.client.HTTPConnection.putheader
    endheaders = http.client.HTTPConnection.endheaders

    @functools.wraps(putrequest)
    def start_record(self: http.client.HTTPConnection, *args: Any, **kwargs: Any) -> None:
        _header_names[self] = set()
        putrequest(self, *args, **kwargs)

    @functools.wraps(putheader)
    def record_header(self: http.client.HTTPConnection, header: str | bytes, *values: Any) -> None:
        _header_names.setdefault(self, set()).add(header_text(header).lower())
        putheader(self, header, *values)

    @functools.wraps(endheaders)
    def add_trace(self: http.client.HTTPConnection, *args: Any, **kwargs: Any) -> None:
        for name, value in _added_headers(
            TRACEPARENT_HEADER in _header_names.get(self, ())
        ).items():
            self.putheader(name, value)
        endheaders(self, *args, **kwargs)

    http.client.HTTPConnection.putrequest = start_record
    http.client.HTTPConnection.putheader = record_header
    http.client.HTTPConnection.endheaders = add_trace


def _add_to(request: _Request) -> None:
    headers = request.headers
    headers.update(_added_headers(TRACEPARENT_HEADER in headers))


def _install_httpx_family(client_class: Any, async_client_class: Any) -> None:
    send = client_class.send
    async_send = async_client_class.send

    @functools.wraps(send)
    def add_trace(self: object, request: _Request, *args: Any, **kwargs: Any) -> Any:
        _add_to(request)
        return send(self, request, *args, **kwargs)

    @functools.wraps(async_send)
    async def add_trace_async(self: object, request: _Request, *args: Any, **kwargs: Any) -> Any:
        _add_to(request)
        return await async_send(self, request, *args, **kwargs)

    client_class.send = add_trace
    async_client_class.send = add_trace_async


def _install_httpx() -> None:
    import httpx

    _install_httpx_family(httpx.Client, httpx.AsyncClient)


def _install_httpx2() -> None:
    import httpx2

    _install_httpx_family(httpx2.Client, httpx2.AsyncClient)


def _install_websockets() -> None:
    import websockets.client

    client_protocol: Any = websockets.client.ClientProtocol
    send_request = client_protocol.send_request

    @functools.wraps(send_request)
    def add_trace(self: object, request: _Request) -> None:
        _add_to(request)
        send_request(self, request)

    client_protocol.send_request = add_trace


def _carries_trace_into_items(executor: ThreadPoolExecutor) -> bool:
    # `prepare_context` is a classmethod: comparing the bound methods, which are new objects each
    # time, would be false for every pool; the functions under them are the same one when not
    # overridden.
    return (
        _carrying.enabled
        and getattr(type(executor).prepare_context, "__func__", None)
        is ThreadPoolExecutor.prepare_context.__func__
    )


def _install_thread_pool() -> None:
    submit = ThreadPoolExecutor.submit

    @functools.wraps(submit)
    def carry_trace(
        self: ThreadPoolExecutor, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Future[Any]:
        if not _carries_trace_into_items(self):
            return submit(self, fn, *args, **kwargs)
        # Taken even outside a trace: a worker that inherited its starter's context must not hand
        # that trace to the item.
        submitters_context = get_current()

        def run_in_submitters_context() -> Any:
            with attached(submitters_context):
                return fn(*args, **kwargs)

        return submit(self, run_in_submitters_context)

    ThreadPoolExecutor.submit = carry_trace


# Each carrier by the module it needs; one that is not installed is skipped.
_CARRIERS: dict[str, Callable[[], None]] = {
    "http.client": _install_http_client,
    "httpx": _install_httpx,
    "httpx2": _install_httpx2,
    "websockets": _install_websockets,
    "concurrent.futures": _install_thread_pool,
}


def install_trace_carriers() -> None:
    """Install the carriers not yet installed, for each client the process has, and turn it on."""
    for module_name, install in _CARRIERS.items():
        if module_name not in _carrying.installed and importlib.util.find_spec(module_name):
            install()
            _carrying.installed.add(module_name)
    _carrying.enabled = True


def is_enabled() -> bool:
    return _carrying.enabled


def set_enabled(enabled: bool) -> None:
    """Turn the carriers on or off; no carrier is installed or removed."""
    _carrying.enabled = enabled
