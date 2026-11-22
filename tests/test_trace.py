import json
import re

import httpx
import pytest
import structlog
import uvicorn
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagate import get_global_textmap, set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from websockets.asyncio.client import connect

from logline import configure, outgoing_headers
from logline.lifespan import LIFESPAN_LOGGER_NAME
from logline.trace import current_trace_id, current_traceparent, unit_trace
from tests.support.apps import BAGGAGE_PATH, HANDLER_BAGGAGE_KEY, WEBSOCKET_TRACE_PATH, build_app
from tests.support.incoming_trace import (
    INCOMING_SPAN_ID,
    INCOMING_TRACE_ID,
    INCOMING_TRACEPARENT,
    TRACEPARENT_PATTERN,
)
from tests.support.lines import Line, json_lines, lines_of, request_lines, session_lines
from tests.support.propagators import SPAN_HEADER, SingleHeaderPropagator
from tests.support.serving import WEBSOCKET_IMPLEMENTATIONS, serve

_INCOMING_TRACESTATE = "vendor=opaque,other=value"


def _call(path: str, **headers: str) -> dict[str, dict[str, str]]:
    """Make one request to the served app and return its JSON body."""
    body: dict[str, dict[str, str]] = {}

    async def exercise(base_url: str) -> None:
        async with httpx.AsyncClient(base_url=base_url) as client:
            response = await client.get(path, headers=headers)
            body.update(response.json())

    serve(uvicorn.Config(build_app(), log_config=None), exercise)
    return body


def _traceparent(body: dict[str, dict[str, str]]) -> re.Match[str]:
    match = TRACEPARENT_PATTERN.match(body["headers"]["traceparent"])
    assert match
    return match


@pytest.mark.parametrize(
    ("path", "lines_in_the_request"),
    [("/trace", 2), ("/trace-sync", 3)],
)
def test_an_incoming_traceparent_gives_every_line_of_the_request_its_trace_id(
    capsys: pytest.CaptureFixture[str], path: str, lines_in_the_request: int
) -> None:
    configure("trace-service")
    traceparent = INCOMING_TRACEPARENT

    body = _call(path, traceparent=traceparent, tracestate=_INCOMING_TRACESTATE)

    lines = json_lines(capsys.readouterr().out)
    in_request = [
        line for line in lines if line["logger"] not in {"uvicorn.error", LIFESPAN_LOGGER_NAME}
    ]
    assert len(in_request) == lines_in_the_request
    assert {line["trace_id"] for line in in_request} == {INCOMING_TRACE_ID}
    outgoing = _traceparent(body)
    assert outgoing["trace_id"] == INCOMING_TRACE_ID
    assert outgoing["span_id"] != INCOMING_SPAN_ID
    assert outgoing["flags"] == "01"
    assert body["headers"]["tracestate"] == _INCOMING_TRACESTATE


@pytest.mark.parametrize(
    "incoming",
    [
        pytest.param({}, id="absent"),
        pytest.param({"traceparent": "garbage"}, id="malformed"),
        pytest.param(
            {"traceparent": f"00-{'0' * 32}-{INCOMING_SPAN_ID}-01"}, id="all-zero trace id"
        ),
    ],
)
def test_without_a_valid_traceparent_the_request_starts_a_new_trace_with_flags_00(
    capsys: pytest.CaptureFixture[str], incoming: dict[str, str]
) -> None:
    configure("trace-service")

    body = _call("/trace", **incoming)

    (request_line,) = request_lines(json_lines(capsys.readouterr().out))
    match = _traceparent(body)
    assert match["trace_id"] == request_line["trace_id"]
    assert int(match["trace_id"], 16) != 0
    assert int(match["span_id"], 16) != 0
    assert match["flags"] == "00"
    assert "tracestate" not in body["headers"]


def test_the_error_line_of_an_unhandled_exception_carries_the_requests_trace_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("trace-service")

    async def exercise(base_url: str) -> None:
        async with httpx.AsyncClient(base_url=base_url) as client:
            await client.get(
                "/boom",
                headers={
                    "x-secret": "a value",
                    "traceparent": INCOMING_TRACEPARENT,
                },
            )

    serve(uvicorn.Config(build_app(), log_config=None), exercise)

    lines = json_lines(capsys.readouterr().out)
    failures = [line for line in lines_of(lines, "uvicorn.error") if line["level"] == "error"]
    assert [line["trace_id"] for line in [*failures, *request_lines(lines)]] == [
        INCOMING_TRACE_ID,
        INCOMING_TRACE_ID,
    ]


def test_two_requests_without_a_traceparent_get_different_traces() -> None:
    configure("trace-service")

    first = _call("/trace")
    second = _call("/trace")

    assert _traceparent(first)["trace_id"] != _traceparent(second)["trace_id"]


def test_outside_a_request_there_is_no_trace_to_hand_on(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("trace-service")
    _call("/trace")
    capsys.readouterr()

    structlog.get_logger("tests").info("after the request")

    assert outgoing_headers() == {}
    (line,) = json_lines(capsys.readouterr().out)
    assert line["trace_id"] is None


_INCOMING_BAGGAGE = "incoming-key=incoming-value"
_WITHOUT_PROPAGATORS = CompositePropagator([])


@pytest.mark.parametrize("unset", [None, ""], ids=["unset", "empty"])
def test_with_otel_propagators_unset_no_baggage_is_forwarded_or_continued(
    monkeypatch: pytest.MonkeyPatch, unset: str | None
) -> None:
    if unset is not None:
        monkeypatch.setenv("OTEL_PROPAGATORS", unset)
    configure("trace-service")

    body = _call(BAGGAGE_PATH, baggage=_INCOMING_BAGGAGE)

    assert set(body["outgoing"]) == {"traceparent"}
    assert set(body["injected"]) == {"traceparent"}
    assert body["extracted"] == {}


def test_with_trace_context_and_baggage_in_otel_propagators_handler_set_baggage_is_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_PROPAGATORS", "tracecontext,baggage")
    set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    )
    configure("trace-service")

    body = _call(BAGGAGE_PATH)

    for headers in (body["outgoing"], body["injected"]):
        assert HANDLER_BAGGAGE_KEY in headers["baggage"]


@pytest.mark.parametrize("propagators", ["none", "b3", "tracecontext,baggage"])
def test_with_otel_propagators_set_configure_leaves_the_global_propagator_as_it_was(
    monkeypatch: pytest.MonkeyPatch, propagators: str
) -> None:
    monkeypatch.setenv("OTEL_PROPAGATORS", propagators)
    set_global_textmap(_WITHOUT_PROPAGATORS)

    configure("trace-service")

    assert get_global_textmap() is _WITHOUT_PROPAGATORS


def test_configure_takes_the_global_propagator_over_from_another_library_and_twice() -> None:
    set_global_textmap(_WITHOUT_PROPAGATORS)

    configure("trace-service")
    configure("trace-service")

    assert isinstance(get_global_textmap(), TraceContextTextMapPropagator)


@pytest.mark.usefixtures("host_with_otel_propagators_none")
def test_with_otel_propagators_none_a_request_starts_a_new_trace_and_hands_nothing_on(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("trace-service")

    body = _call("/trace", traceparent=INCOMING_TRACEPARENT)

    (request_line,) = request_lines(json_lines(capsys.readouterr().out))
    assert request_line["trace_id"] not in {None, INCOMING_TRACE_ID}
    assert body["headers"] == {}


def test_a_global_propagator_reading_another_header_than_traceparent_continues_the_trace(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OTEL_PROPAGATORS", "host-propagator")
    set_global_textmap(SingleHeaderPropagator())
    configure("trace-service")

    body = _call("/trace", **{SPAN_HEADER: f"{INCOMING_TRACE_ID}-{INCOMING_SPAN_ID}"})

    (request_line,) = request_lines(json_lines(capsys.readouterr().out))
    assert request_line["trace_id"] == INCOMING_TRACE_ID
    assert body["headers"][SPAN_HEADER].startswith(f"{INCOMING_TRACE_ID}-")


@pytest.mark.usefixtures("host_with_otel_propagators_none")
def test_with_otel_propagators_none_a_unit_starts_a_new_trace_and_hands_nothing_on() -> None:
    configure("trace-service")

    with unit_trace(INCOMING_TRACEPARENT):
        assert current_trace_id() not in {None, INCOMING_TRACE_ID}
        assert current_traceparent() is None


type _Frame = dict[str, str | bool | None]


@pytest.fixture(params=WEBSOCKET_IMPLEMENTATIONS)
def implementation(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _sessions_upgraded_with(
    capsys: pytest.CaptureFixture[str], implementation: str, upgrades: list[dict[str, str]]
) -> list[tuple[dict[str, str | dict[str, str]], Line]]:
    """Open the trace socket per upgrade's headers; return each one's frame and session line."""
    configure("trace-service")
    frames: list[dict[str, str | dict[str, str]]] = []

    async def exercise(base_url: str) -> None:
        for headers in upgrades:
            url = base_url.replace("http", "ws") + WEBSOCKET_TRACE_PATH
            async with connect(url, additional_headers=headers) as websocket:
                frames.append(json.loads(await websocket.recv()))
                await websocket.wait_closed()

    serve(uvicorn.Config(build_app(), log_config=None, ws=implementation), exercise)
    lines = session_lines(json_lines(capsys.readouterr().out))
    return list(zip(frames, lines, strict=True))


def test_an_incoming_traceparent_on_the_upgrade_gives_the_session_its_trace_id_and_parent_span(
    capsys: pytest.CaptureFixture[str], implementation: str
) -> None:
    upgrade = {
        "traceparent": INCOMING_TRACEPARENT,
        "tracestate": _INCOMING_TRACESTATE,
    }

    ((frame, session_line),) = _sessions_upgraded_with(capsys, implementation, [upgrade])

    outgoing = TRACEPARENT_PATTERN.match(str(frame["traceparent"]))
    assert outgoing
    assert outgoing["trace_id"] == INCOMING_TRACE_ID
    assert outgoing["span_id"] != INCOMING_SPAN_ID
    assert outgoing["flags"] == "01"
    assert frame["headers"] == {
        "traceparent": frame["traceparent"],
        "tracestate": _INCOMING_TRACESTATE,
    }
    assert session_line["trace_id"] == INCOMING_TRACE_ID
    assert session_line["span_id"] == outgoing["span_id"]
    assert session_line["parent_span_id"] == INCOMING_SPAN_ID


@pytest.mark.parametrize(
    "incoming",
    [
        pytest.param({}, id="absent"),
        pytest.param({"traceparent": "garbage"}, id="malformed"),
        pytest.param(
            {"traceparent": f"00-{'0' * 32}-{INCOMING_SPAN_ID}-01"}, id="all-zero trace id"
        ),
    ],
)
def test_without_a_valid_traceparent_the_session_starts_a_new_trace_with_flags_00(
    capsys: pytest.CaptureFixture[str], implementation: str, incoming: dict[str, str]
) -> None:
    ((frame, session_line),) = _sessions_upgraded_with(capsys, implementation, [incoming])

    match = TRACEPARENT_PATTERN.match(str(frame["traceparent"]))
    assert match
    assert match["trace_id"] == session_line["trace_id"]
    assert int(match["trace_id"], 16) != 0
    assert match["flags"] == "00"
    assert "tracestate" not in frame["headers"]


def test_two_sessions_without_a_traceparent_get_different_traces(
    capsys: pytest.CaptureFixture[str], implementation: str
) -> None:
    (_, first), (_, second) = _sessions_upgraded_with(capsys, implementation, [{}, {}])

    assert first["trace_id"] != second["trace_id"]


def _play_turns(
    capsys: pytest.CaptureFixture[str], implementation: str, turns: list[dict[str, str | None]]
) -> tuple[list[tuple[_Frame, _Frame]], list[Line], Line]:
    """Send each turn to the served socket; return its frames, its lines and the session line."""
    configure("trace-service")
    logger_name = "tests.turns"
    frames: list[tuple[_Frame, _Frame]] = []

    async def exercise(base_url: str) -> None:
        async with connect(base_url.replace("http", "ws") + "/turns") as websocket:
            for turn in turns:
                await websocket.send(json.dumps({**turn, "logger": logger_name}))
                inside = json.loads(await websocket.recv())
                after = json.loads(await websocket.recv())
                frames.append((inside, after))

    serve(uvicorn.Config(build_app(), log_config=None, ws=implementation), exercise)
    lines = json_lines(capsys.readouterr().out)
    (session_line,) = session_lines(lines)
    return frames, lines_of(lines, logger_name), session_line


def _turn(traceparent: str | None = None, *, failure: str | None = None) -> dict[str, str | None]:
    return {"traceparent": traceparent, "failure": failure}


def test_a_turn_that_raises_leaves_the_sessions_trace_on_the_sockets_later_lines(
    capsys: pytest.CaptureFixture[str], implementation: str
) -> None:
    incoming = INCOMING_TRACEPARENT

    frames, lines, session_line = _play_turns(
        capsys, implementation, [_turn(incoming, failure="the turn failed"), _turn()]
    )

    session_trace_id = session_line["trace_id"]
    assert isinstance(session_trace_id, str)
    assert session_trace_id != INCOMING_TRACE_ID
    (_, failed_after), (_, later_after) = frames
    assert failed_after["raised"] == "the turn failed"
    assert failed_after["same"] is True
    assert later_after["raised"] is None
    after_traceparents = [
        TRACEPARENT_PATTERN.match(str(after["traceparent"]))
        for after in (failed_after, later_after)
    ]
    assert [match and match["trace_id"] for match in after_traceparents] == [session_trace_id] * 2
    failed_inside, failed_after_line, later_inside, later_after_line = lines
    assert failed_inside["trace_id"] == INCOMING_TRACE_ID
    assert failed_after_line["trace_id"] == session_trace_id
    assert later_inside["trace_id"] not in (None, INCOMING_TRACE_ID, session_trace_id)
    assert later_after_line["trace_id"] == session_trace_id


def test_two_turns_carrying_one_traceparent_log_that_trace_id_and_hand_on_the_senders_span(
    capsys: pytest.CaptureFixture[str], implementation: str
) -> None:
    incoming = INCOMING_TRACEPARENT

    frames, lines, _ = _play_turns(capsys, implementation, [_turn(incoming), _turn(incoming)])

    inside_lines = [line for line in lines if line["event"] == "inside the turn"]
    assert [line["trace_id"] for line in inside_lines] == [INCOMING_TRACE_ID] * 2
    outgoing = [TRACEPARENT_PATTERN.match(str(inside["traceparent"])) for inside, _ in frames]
    assert [match and match["trace_id"] for match in outgoing] == [INCOMING_TRACE_ID] * 2
    assert {match and match["span_id"] for match in outgoing} == {INCOMING_SPAN_ID}


@pytest.mark.parametrize(
    "incoming",
    [None, "garbage", f"00-{'0' * 32}-{INCOMING_SPAN_ID}-01"],
    ids=["absent", "malformed", "all-zero trace id"],
)
def test_a_turn_without_a_valid_traceparent_starts_a_new_trace_with_flags_00(
    capsys: pytest.CaptureFixture[str], implementation: str, incoming: str | None
) -> None:
    frames, lines, _ = _play_turns(capsys, implementation, [_turn(incoming)])

    match = TRACEPARENT_PATTERN.match(str(frames[0][0]["traceparent"]))
    assert match
    assert lines[0]["trace_id"] == match["trace_id"]
    assert int(match["trace_id"], 16) != 0
    assert match["flags"] == "00"
