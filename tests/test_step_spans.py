import json
import re

import pytest
import uvicorn
from websockets.asyncio.client import connect

from logline import REQUEST_EVENT, configure
from logline.standard import CLOSING_LINE_KEYS
from tests.support.apps import build_app
from tests.support.lines import Line, json_lines, lines_of, request_lines, session_lines
from tests.support.serving import WEBSOCKET_IMPLEMENTATIONS, get, serve, serve_requests_answered
from tests.support.span_routes import CALLEE_PATH, TURNS_PATH
from tests.support.step_routes import STEP_EVENT, STEP_LOGGER

_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
_SPAN_ID = "00f067aa0ba902b7"
_TRACEPARENT = f"00-{_TRACE_ID}-{_SPAN_ID}-01"
_HEX_SPAN_ID = re.compile(r"[0-9a-f]{16}")
_OPENTELEMETRY_CONTEXT_LOGGER = "opentelemetry.context"


def _request_of(lines: list[Line], route: str) -> Line:
    (line,) = [line for line in request_lines(lines) if line["route"] == route]
    return line


def _span_of_traceparent(traceparent: object) -> str:
    match = re.fullmatch(r"00-[0-9a-f]{32}-([0-9a-f]{16})-[0-9a-f]{2}", str(traceparent))
    assert match
    return match[1]


@pytest.mark.parametrize(
    "path",
    [
        "/step/ok",
        "/step/failed",
        "/step/failed-escaping",
        "/step/cancelled",
        "/step/thread",
        "/step/sync-endpoint",
        "/span/task",
        "/step/generator-finished",
        "/step/generator-closed",
        "/span/generator-dropped",
        "/span/context-manager",
        "/step/end",
        "/step/sync-end",
        "/step/end-in-block",
        "/step/end-cancelled",
        "/step/sync-end-cancelled",
        "/step/end-cancelled-in-block",
    ],
)
def test_a_step_line_names_its_own_span_and_the_requests_span_as_its_parent(
    capsys: pytest.CaptureFixture[str], path: str
) -> None:
    _, lines, _ = serve_requests_answered(capsys, [get(path, traceparent=_TRACEPARENT)])

    (request,) = request_lines(lines)
    (step,) = lines_of(lines, STEP_LOGGER)
    assert step["trace_id"] == request["trace_id"] == _TRACE_ID
    assert step["parent_span_id"] == request["span_id"]
    assert _HEX_SPAN_ID.fullmatch(str(step["span_id"]))
    assert step["span_id"] != request["span_id"]
    assert lines_of(lines, _OPENTELEMETRY_CONTEXT_LOGGER) == []


@pytest.mark.parametrize(
    ("incoming", "parent_span_id"),
    [
        pytest.param({"traceparent": _TRACEPARENT}, _SPAN_ID, id="incoming"),
        pytest.param({}, None, id="new-trace"),
    ],
)
def test_a_request_line_names_its_span_with_the_incoming_span_as_parent_or_null(
    capsys: pytest.CaptureFixture[str], incoming: dict[str, str], parent_span_id: str | None
) -> None:
    _, lines, answers = serve_requests_answered(capsys, [get("/trace", **incoming)])

    (line,) = request_lines(lines)
    assert line["parent_span_id"] == parent_span_id
    outgoing = answers[0].json()["headers"]["traceparent"]
    assert line["span_id"] == _span_of_traceparent(outgoing)


def test_a_step_inside_a_step_names_the_outer_step_as_its_parent_and_hands_on_its_own_span(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines, answers = serve_requests_answered(
        capsys, [get("/span/nested", traceparent=_TRACEPARENT)]
    )

    inner, outer = lines_of(lines, STEP_LOGGER)
    (request,) = request_lines(lines)
    assert (inner["position"], outer["position"]) == ("inner", "outer")
    assert inner["parent_span_id"] == outer["span_id"]
    assert outer["parent_span_id"] == request["span_id"]
    assert _span_of_traceparent(answers[0].json()["traceparent"]) == inner["span_id"]


def test_a_callee_requested_from_inside_a_step_names_the_step_as_its_parent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines, _ = serve_requests_answered(capsys, [get("/span/call", traceparent=_TRACEPARENT)])

    (step,) = lines_of(lines, STEP_LOGGER)
    callee = _request_of(lines, CALLEE_PATH)
    assert callee["parent_span_id"] == step["span_id"]
    assert callee["trace_id"] == _TRACE_ID
    assert step["parent_span_id"] == _request_of(lines, "/span/call")["span_id"]


@pytest.mark.parametrize("how", ["exit", "end", "end-cancelled"])
def test_a_step_that_ended_leaves_the_requests_span_current_for_the_next_step(
    capsys: pytest.CaptureFixture[str], how: str
) -> None:
    _, lines, _ = serve_requests_answered(
        capsys, [get(f"/span/after/{how}", traceparent=_TRACEPARENT)]
    )

    (request,) = request_lines(lines)
    first, second = lines_of(lines, STEP_LOGGER)
    assert (first["position"], second["position"]) == ("first", "second")
    assert {first["parent_span_id"], second["parent_span_id"]} == {request["span_id"]}


def test_a_field_named_like_a_span_key_never_reaches_the_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines, _ = serve_requests_answered(capsys, [get("/step/timer-named-fields")])

    (line,) = lines_of(lines, STEP_LOGGER)
    assert "caller" not in {line["span_id"], line["parent_span_id"]}


def test_only_the_closing_lines_carry_the_standards_span_keys(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines, _ = serve_requests_answered(capsys, [get("/trace"), get("/step/ok")])

    closing = [line for line in lines if line["event"] in {REQUEST_EVENT, STEP_EVENT}]
    others = [line for line in lines if line not in closing]
    assert len(closing) == 3
    assert others
    assert all(CLOSING_LINE_KEYS <= line.keys() for line in closing)
    assert not any(CLOSING_LINE_KEYS & line.keys() for line in others)


type _Turn = dict[str, str | None]


def _turn(action: str, traceparent: str | None) -> _Turn:
    return {"do": action, "traceparent": traceparent}


def _play(
    capsys: pytest.CaptureFixture[str], implementation: str, turns: list[_Turn]
) -> tuple[list[_Turn], list[Line]]:
    """Send each turn to the served socket; return its answers and every line written."""
    configure("span-service")
    answers: list[_Turn] = []

    async def exercise(base_url: str) -> None:
        async with connect(base_url.replace("http", "ws") + TURNS_PATH) as websocket:
            for turn in turns:
                await websocket.send(json.dumps(turn))
                answers.append(json.loads(await websocket.recv()))

    serve(uvicorn.Config(build_app(), log_config=None, ws=implementation), exercise)
    return answers, json_lines(capsys.readouterr().out)


@pytest.mark.parametrize("implementation", WEBSOCKET_IMPLEMENTATIONS)
def test_a_step_in_a_turn_names_the_senders_span_as_its_parent(
    capsys: pytest.CaptureFixture[str], implementation: str
) -> None:
    answers, lines = _play(capsys, implementation, [_turn("step", _TRACEPARENT)])

    (step,) = lines_of(lines, STEP_LOGGER)
    assert (step["trace_id"], step["parent_span_id"]) == (_TRACE_ID, _SPAN_ID)
    assert _span_of_traceparent(answers[0]["traceparent"]) == step["span_id"]


@pytest.mark.parametrize("implementation", WEBSOCKET_IMPLEMENTATIONS)
def test_a_step_in_a_turn_without_a_valid_traceparent_names_the_turns_fresh_span_as_its_parent(
    capsys: pytest.CaptureFixture[str], implementation: str
) -> None:
    answers, lines = _play(capsys, implementation, [_turn("step", None)])

    (step,) = lines_of(lines, STEP_LOGGER)
    assert step["trace_id"] == str(answers[0]["traceparent"]).split("-")[1]
    assert _HEX_SPAN_ID.fullmatch(str(step["parent_span_id"]))
    (session,) = session_lines(lines)
    assert step["parent_span_id"] != session["span_id"]


@pytest.mark.parametrize("implementation", WEBSOCKET_IMPLEMENTATIONS)
def test_a_step_closed_in_another_turn_keeps_its_own_trace(
    capsys: pytest.CaptureFixture[str], implementation: str
) -> None:
    other_trace = "0af7651916cd43dd8448eb211c80319c"
    other_span = "b7ad6b7169203331"
    later_span = "a3ce929d0e0e4736"

    _, lines = _play(
        capsys,
        implementation,
        [
            _turn("enter", _TRACEPARENT),
            _turn("exit", f"00-{other_trace}-{other_span}-01"),
            _turn("step", f"00-{other_trace}-{later_span}-01"),
        ],
    )

    closed, later = lines_of(lines, STEP_LOGGER)
    assert (closed["trace_id"], closed["parent_span_id"]) == (_TRACE_ID, _SPAN_ID)
    assert (later["trace_id"], later["parent_span_id"]) == (other_trace, later_span)
    assert lines_of(lines, _OPENTELEMETRY_CONTEXT_LOGGER) == []


@pytest.mark.parametrize("implementation", WEBSOCKET_IMPLEMENTATIONS)
def test_a_step_run_after_one_closed_in_another_turn_belongs_to_the_current_turn(
    capsys: pytest.CaptureFixture[str], implementation: str
) -> None:
    other_trace = "0af7651916cd43dd8448eb211c80319c"
    other_span = "b7ad6b7169203331"

    _, lines = _play(
        capsys,
        implementation,
        [
            _turn("enter", _TRACEPARENT),
            _turn("exit-then-step", f"00-{other_trace}-{other_span}-01"),
        ],
    )

    closed, later = lines_of(lines, STEP_LOGGER)
    assert (closed["trace_id"], closed["parent_span_id"]) == (_TRACE_ID, _SPAN_ID)
    assert (later["trace_id"], later["parent_span_id"]) == (other_trace, other_span)


@pytest.mark.parametrize("implementation", WEBSOCKET_IMPLEMENTATIONS)
def test_a_session_line_names_its_span_and_the_upgrades_span_as_parent_or_null(
    capsys: pytest.CaptureFixture[str], implementation: str
) -> None:
    _, lines = _play(capsys, implementation, [_turn("step", None)])

    (session,) = session_lines(lines)
    assert _HEX_SPAN_ID.fullmatch(str(session["span_id"]))
    assert session["parent_span_id"] is None
