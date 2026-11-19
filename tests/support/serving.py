import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import TracebackType

import httpx
import pytest
import uvicorn

from logline import configure
from tests.support.apps import build_app
from tests.support.lines import Line, json_lines

HTTP_IMPLEMENTATIONS = ["h11", "httptools"]
# What uvicorn's default `ws="auto"` resolves to with `websockets` installed, and wsproto.
WEBSOCKET_IMPLEMENTATIONS = ["websockets-sansio", "wsproto"]


def serve(config: uvicorn.Config, exercise: Callable[[str], Awaitable[None]]) -> None:
    """Serve `config` with a real uvicorn server while `exercise` runs, then stop it.

    `exercise` gets the server's base URL. The listening socket exists before the server runs,
    so a request made at once waits in its backlog and nothing needs polling. A config that leaves
    `http` on "auto" runs h11, so an installed httptools never changes which parser a test means.
    """

    async def exercise_url(base_url: str, _server: uvicorn.Server) -> None:
        await exercise(base_url)

    serve_with_server(config, exercise_url)


def serve_with_server(
    config: uvicorn.Config, exercise: Callable[[str, uvicorn.Server], Awaitable[None]]
) -> None:
    """Serve like `serve`, and hand `exercise` the server too, for a test that stops it itself."""
    if config.http == "auto":
        config.http = "h11"

    async def run() -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(config)
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            await exercise(base_url, server)
        finally:
            server.should_exit = True
            await serving
            listener.close()

    asyncio.run(run())


class WrittenEvents(logging.Handler):
    """Tells a test that the named logger wrote an event, after its filters ran.

    A test that must act once the server has logged something waits on `seen`, so it never sleeps.
    """

    def __init__(self, logger_name: str) -> None:
        super().__init__()
        self._logger = logging.getLogger(logger_name)
        self._seen: dict[tuple[str, str | None], asyncio.Event] = {}

    def seen(self, event: str, reason: str | None = None) -> asyncio.Event:
        return self._seen.setdefault((event, reason), asyncio.Event())

    def emit(self, record: logging.LogRecord) -> None:
        self.seen(str(record.msg), record.__dict__.get("reason")).set()

    def __enter__(self) -> WrittenEvents:
        self._logger.addHandler(self)
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._logger.removeHandler(self)


@dataclass(frozen=True)
class Call:
    path: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""


def get(path: str, **headers: str) -> Call:
    return Call(path, headers=headers)


def serve_requests_answered(
    capsys: pytest.CaptureFixture[str],
    calls: list[Call],
    *,
    config: uvicorn.Config | None = None,
) -> tuple[str, list[Line], list[httpx.Response]]:
    """Serve the app, make `calls` one after another, return stdout, its lines and the answers."""
    configure("request-service")
    answers: list[httpx.Response] = []

    async def exercise(base_url: str) -> None:
        async with httpx.AsyncClient(base_url=base_url) as client:
            for call in calls:
                answers.append(
                    await client.request(
                        call.method, call.path, headers=call.headers, content=call.content
                    )
                )

    serve(config or uvicorn.Config(build_app(), log_config=None), exercise)
    output = capsys.readouterr().out
    return output, json_lines(output), answers


def serve_requests(
    capsys: pytest.CaptureFixture[str],
    calls: list[Call],
    *,
    config: uvicorn.Config | None = None,
) -> tuple[str, list[Line]]:
    """Serve the app, make `calls` one after another, return stdout and its parsed lines."""
    output, lines, _ = serve_requests_answered(capsys, calls, config=config)
    return output, lines
