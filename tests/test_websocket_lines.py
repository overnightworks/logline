import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidHandshake

from logline import Outcome, configure
from logline.request import REQUEST_LOGGER_NAME
from logline.standard import BASE_KEYS
from tests.support.apps import (
    HOST_CLIENT_IP,
    HOST_ROUTE,
    SUB,
    WEBSOCKET_CANCELLED_PATH,
    WEBSOCKET_CLIENT_CLOSES_PATH,
    WEBSOCKET_CLOSED_PATH,
    WEBSOCKET_DENIED_PATH,
    WEBSOCKET_DENIED_STATUS,
    WEBSOCKET_RAISES_PATH,
    WEBSOCKET_SERVER_CLOSES_PATH,
    WEBSOCKET_SILENT_PATH,
    build_app,
)
from tests.support.lines import Line, json_lines, lines_of
from tests.support.serving import serve

_SESSION_EVENT = "websocket.ended"
_CLIENT_CLOSE_CODE = 4001
_UVICORN_LOGGER = "uvicorn.error"

type Client = Callable[[str], Awaitable[None]]


async def _read_until_the_server_closes(url: str) -> None:
    async with connect(url) as websocket:
        await websocket.wait_closed()


async def _close_from_the_client(url: str) -> None:
    async with connect(url) as websocket:
        await websocket.close(_CLIENT_CLOSE_CODE)


async def _try_the_upgrade(url: str) -> None:
    # A refusal reaches the client as InvalidStatus, or as InvalidMessage where the server
    # writes the denial response with a duplicate Content-Length header.
    with pytest.raises(InvalidHandshake):
        await connect(url)


@dataclass(frozen=True)
class Session:
    path: str
    client: Client
    expected: Line


_OK_BY_SERVER = Session(
    WEBSOCKET_SERVER_CLOSES_PATH,
    _read_until_the_server_closes,
    {
        "outcome": Outcome.OK,
        "route": HOST_ROUTE,
        "sub": SUB,
        "ip": HOST_CLIENT_IP,
        "close_code": 1000,
    },
)
_OK_BY_CLIENT = Session(
    WEBSOCKET_CLIENT_CLOSES_PATH,
    _close_from_the_client,
    {
        "outcome": Outcome.OK,
        "route": WEBSOCKET_CLIENT_CLOSES_PATH,
        "sub": None,
        "close_code": _CLIENT_CLOSE_CODE,
    },
)
_RAISED = Session(
    WEBSOCKET_RAISES_PATH,
    _read_until_the_server_closes,
    {"outcome": Outcome.FAILED, "close_code": None},
)
_CANCELLED = Session(
    WEBSOCKET_CANCELLED_PATH,
    _read_until_the_server_closes,
    {"outcome": Outcome.CANCELLED, "close_code": None},
)
_DENIED = Session(
    WEBSOCKET_DENIED_PATH,
    _try_the_upgrade,
    {"outcome": Outcome.REFUSED, "status": WEBSOCKET_DENIED_STATUS, "close_code": None},
)
_CLOSED_BEFORE_ACCEPT = Session(
    WEBSOCKET_CLOSED_PATH,
    _try_the_upgrade,
    {"outcome": Outcome.REFUSED, "status": 403, "close_code": None},
)
_NOT_ANSWERED = Session(
    WEBSOCKET_SILENT_PATH,
    _try_the_upgrade,
    {"outcome": Outcome.FAILED, "status": 500, "close_code": None},
)

_SESSIONS = [
    pytest.param(_OK_BY_SERVER, id="accepted, closed by the server"),
    pytest.param(_OK_BY_CLIENT, id="accepted, closed by the client"),
    pytest.param(_RAISED, id="accepted, the app raised"),
    pytest.param(_CANCELLED, id="accepted, the app was cancelled"),
    pytest.param(_DENIED, id="refused by a denial response"),
    pytest.param(_CLOSED_BEFORE_ACCEPT, id="refused by a close before accept"),
    pytest.param(_NOT_ANSWERED, id="the app returned without answering"),
]
# The legacy implementation runs the handshake outside the app task, so its own refusal line
# stays; the others run in the app task, where the session line replaces it.
_REFUSAL_EVENT = "websocket_upgrade.refused"
_UNCODED_EVENTS = {"connection open", "websocket_handshake.failed", "uvicorn.unknown_record"}
_IMPLEMENTATIONS = [
    pytest.param(
        "websockets-sansio",
        {*_UNCODED_EVENTS, _REFUSAL_EVENT},
        id="sansio",
    ),
    pytest.param("wsproto", {*_UNCODED_EVENTS, _REFUSAL_EVENT}, id="wsproto"),
    pytest.param("websockets", _UNCODED_EVENTS, id="legacy"),
]


def _serve_sessions(sessions: list[Session], implementation: str, *, concurrently: bool) -> None:
    async def exercise(base_url: str) -> None:
        urls = [(base_url.replace("http", "ws") + session.path, session) for session in sessions]
        if concurrently:
            await asyncio.gather(*(session.client(url) for url, session in urls))
        else:
            for url, session in urls:
                await session.client(url)

    serve(uvicorn.Config(build_app(), log_config=None, ws=implementation), exercise)


def _session_lines(lines: list[Line]) -> list[Line]:
    return [
        line for line in lines_of(lines, REQUEST_LOGGER_NAME) if line["event"] == _SESSION_EVENT
    ]


@pytest.mark.parametrize(("implementation", "absent_events"), _IMPLEMENTATIONS)
@pytest.mark.parametrize("session", _SESSIONS)
def test_a_websocket_session_writes_one_line_when_it_ends(
    capsys: pytest.CaptureFixture[str],
    session: Session,
    implementation: str,
    absent_events: set[str],
) -> None:
    configure("socket-service")

    _serve_sessions([session], implementation, concurrently=False)

    lines = json_lines(capsys.readouterr().out)
    (line,) = _session_lines(lines)
    assert session.expected.items() <= line.items()
    assert line.keys() <= {*BASE_KEYS, *session.expected, "duration_ms", "route", "sub", "ip"}
    assert isinstance(line["duration_ms"], int | float)
    assert not [line for line in lines if str(line["event"]).startswith("request.")]
    server_events = {line["event"] for line in lines_of(lines, _UVICORN_LOGGER)}
    assert not absent_events & {str(event) for event in server_events}


def test_concurrent_sessions_each_write_their_own_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("socket-service")

    _serve_sessions(
        [_DENIED, _NOT_ANSWERED, _OK_BY_SERVER] * 2, "websockets-sansio", concurrently=True
    )

    lines = json_lines(capsys.readouterr().out)
    assert Counter((line["outcome"], line.get("status")) for line in _session_lines(lines)) == {
        (Outcome.REFUSED, WEBSOCKET_DENIED_STATUS): 2,
        (Outcome.FAILED, 500): 2,
        (Outcome.OK, None): 2,
    }
    assert not [
        line
        for line in lines_of(lines, _UVICORN_LOGGER)
        if str(line["event"]) in {"websocket_handshake.failed", _REFUSAL_EVENT}
    ]
