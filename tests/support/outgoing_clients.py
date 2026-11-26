"""Real HTTP clients and a real server that records the headers each request arrived with."""

import asyncio
import http.client
import io
import logging
import tempfile
import urllib.request
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import boto3
import botocore.session
import httpx
import httpx2
import urllib3

from logline import StepTimer, unit_trace
from logline.trace import request_trace
from tests.support.incoming_trace import INCOMING_TRACEPARENT
from tests.support.serving_in_a_thread import serving_in_a_thread
from tests.support.step_routes import STEP_EVENT, STEP_LOGGER

type Headers = dict[str, str]
type Call = Callable[[str, Headers], None]
type Transfer = Callable[[str], None]
type ReceivedHeaders = dict[str, list[str]]

TRACEPARENT = "traceparent"
TRACESTATE = "tracestate"
INCOMING_TRACESTATE = "vendor=incoming"
A_CALLERS_TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


@dataclass
class Received:
    """One request as the server saw it: method, headers by lower-case name and the peer's port."""

    method: str
    headers: ReceivedHeaders
    client_port: int


@dataclass
class EchoServer:
    url: str
    received: list[Received] = field(default_factory=list)

    @property
    def last(self) -> ReceivedHeaders:
        return self.received[-1].headers


def _handler_recording(server: EchoServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # Keep-alive: a client's connection serves its next request.
        protocol_version = "HTTP/1.1"

        def _answer(self) -> None:
            headers: ReceivedHeaders = {}
            for name, value in self.headers.items():
                headers.setdefault(name.lower(), []).append(value)
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            server.received.append(Received(self.command, headers, self.client_address[1]))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = _answer
        do_HEAD = _answer
        do_PUT = _answer

        def log_message(self, format: str, *args: object) -> None:
            """The standard library's access line is not under test."""

    return Handler


@contextmanager
def echo_server() -> Iterator[EchoServer]:
    """Serve with a real `ThreadingHTTPServer` in a thread; every request is recorded."""
    echo = EchoServer(url="")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_recording(echo))
    server.daemon_threads = True
    echo.url = f"http://127.0.0.1:{server.server_port}"
    with serving_in_a_thread(server):
        yield echo


def _with_http_client(url: str, headers: Headers) -> None:
    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port)
    try:
        connection.request("GET", "/", headers=headers)
        connection.getresponse().read()
    finally:
        connection.close()


def _with_urllib_request(url: str, headers: Headers) -> None:
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 - a local http url
    with urllib.request.urlopen(request) as response:  # noqa: S310 - a local http url
        response.read()


def _with_urllib3(url: str, headers: Headers) -> None:
    with urllib3.PoolManager() as pool:
        pool.request("GET", url, headers=headers)


def _with_botocore(url: str, headers: Headers) -> None:
    client = botocore.session.get_session().create_client(
        "s3",
        endpoint_url=url,
        region_name="us-east-1",
        aws_access_key_id="dummy-key-id",
        aws_secret_access_key="dummy-secret",
    )

    def add_caller_headers(request: object, **_: object) -> None:
        for name, value in headers.items():
            request.headers.add_header(name, value)  # pyright: ignore[reportAttributeAccessIssue]

    client.meta.events.register("before-sign.s3.HeadBucket", add_caller_headers)
    client.head_bucket(Bucket="a-bucket")


def _with_httpx_family(client_module: ModuleType) -> tuple[Call, Call]:
    def sync(url: str, headers: Headers) -> None:
        with client_module.Client() as client:
            client.get(url, headers=headers)

    def asynchronous(url: str, headers: Headers) -> None:
        async def call() -> None:
            async with client_module.AsyncClient() as client:
                await client.get(url, headers=headers)

        asyncio.run(call())

    return sync, asynchronous


_HTTPX_CALL, _HTTPX_ASYNC_CALL = _with_httpx_family(httpx)
_HTTPX2_CALL, _HTTPX2_ASYNC_CALL = _with_httpx_family(httpx2)


def _in_a_pool_with_submit(url: str, headers: Headers) -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_with_http_client, url, headers).result()


def _in_a_pool_with_map(url: str, headers: Headers) -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        list(pool.map(_with_http_client, [url], [headers]))


def _in_run_in_executor(url: str, headers: Headers) -> None:
    async def call() -> None:
        await asyncio.get_running_loop().run_in_executor(None, _with_http_client, url, headers)

    asyncio.run(call())


# Every covered client by a name that is its test id.
CLIENTS: dict[str, Call] = {
    "http.client": _with_http_client,
    "urllib.request": _with_urllib_request,
    "urllib3": _with_urllib3,
    "botocore": _with_botocore,
    "httpx": _HTTPX_CALL,
    "httpx-async": _HTTPX_ASYNC_CALL,
    "httpx2": _HTTPX2_CALL,
    "httpx2-async": _HTTPX2_ASYNC_CALL,
    "thread-pool-submit": _in_a_pool_with_submit,
    "thread-pool-map": _in_a_pool_with_map,
    "run-in-executor": _in_run_in_executor,
}


def _s3_client(url: str) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=url,
        region_name="us-east-1",
        aws_access_key_id="dummy-key-id",
        aws_secret_access_key="dummy-secret",
    )


def _upload_file(url: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory, "source")
        source.write_bytes(b"content")
        _s3_client(url).upload_file(str(source), "a-bucket", "a-key")


def _download_file(url: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        _s3_client(url).download_file("a-bucket", "a-key", str(Path(directory, "target")))


def _upload_fileobj(url: str) -> None:
    _s3_client(url).upload_fileobj(io.BytesIO(b"content"), "a-bucket", "a-key")


def _download_fileobj(url: str) -> None:
    _s3_client(url).download_fileobj("a-bucket", "a-key", io.BytesIO())


# Each managed transfer of boto3, which sends from the pool of its own, by a name that is its test
# id, and the methods of the requests it sends.
TRANSFERS: dict[str, tuple[Transfer, set[str]]] = {
    "upload_file": (_upload_file, {"PUT"}),
    "download_file": (_download_file, {"HEAD", "GET"}),
    "upload_fileobj": (_upload_fileobj, {"PUT"}),
    "download_fileobj": (_download_fileobj, {"HEAD", "GET"}),
}


@contextmanager
def inside_a_request() -> Iterator[None]:
    with request_trace([(TRACEPARENT, INCOMING_TRACEPARENT), (TRACESTATE, INCOMING_TRACESTATE)]):
        yield


@contextmanager
def inside_a_step() -> Iterator[None]:
    with inside_a_request(), StepTimer(STEP_EVENT, logging.getLogger(STEP_LOGGER)):
        yield


@contextmanager
def inside_a_turn() -> Iterator[None]:
    with unit_trace(INCOMING_TRACEPARENT):
        yield


# Each way a trace is current, by a name that is its test id.
SCOPES = {"request": inside_a_request, "step": inside_a_step, "turn": inside_a_turn}
