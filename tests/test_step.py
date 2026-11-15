import logging

import pytest

from logline import Outcome, StepTimer
from tests.support.apps import (
    STEP_AGENT_ID,
    STEP_EVENT,
    STEP_FAILURE_MESSAGE,
    STEP_LOGGER,
    STEP_REFUSAL_REASON,
    STEP_SECOND_MARK_DELAY_SECONDS,
)
from tests.support.lines import Line, lines_of, request_lines
from tests.support.serving import get, serve_requests, serve_requests_answered

_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
_TRACEPARENT = f"00-{_TRACE_ID}-00f067aa0ba902b7-01"
_INFO, _WARNING, _ERROR = "info", "warning", "error"


def _step_line(lines: list[Line]) -> Line:
    (line,) = lines_of(lines, STEP_LOGGER)
    return line


@pytest.mark.parametrize(
    ("path", "level", "outcome", "extra"),
    [
        pytest.param("/step/ok", _INFO, Outcome.OK, {}, id="succeeds"),
        pytest.param(
            "/step/failed", _ERROR, Outcome.FAILED, {"error_type": "ValueError"}, id="fails"
        ),
        pytest.param(
            "/step/failed-handled",
            _WARNING,
            Outcome.FAILED,
            {"error_type": "ValueError"},
            id="fails-and-its-unit-goes-on",
        ),
        pytest.param("/step/degraded", _WARNING, Outcome.OK, {}, id="degraded"),
        pytest.param(
            "/step/refused",
            _INFO,
            Outcome.REFUSED,
            {"reason": STEP_REFUSAL_REASON},
            id="refused",
        ),
        pytest.param(
            "/step/refused-degraded",
            _WARNING,
            Outcome.REFUSED,
            {"reason": STEP_REFUSAL_REASON},
            id="refused-and-degraded",
        ),
        pytest.param("/step/cancelled", _INFO, Outcome.CANCELLED, {}, id="cancelled"),
        pytest.param("/step/generator-closed", _INFO, Outcome.CANCELLED, {}, id="generator-closed"),
        pytest.param("/step/generator-finished", _INFO, Outcome.OK, {}, id="generator-finished"),
        pytest.param("/step/thread", _INFO, Outcome.OK, {}, id="sync-form-on-a-worker-thread"),
        pytest.param(
            "/step/thread-failed",
            _ERROR,
            Outcome.FAILED,
            {"error_type": "ValueError"},
            id="sync-form-failing-on-a-worker-thread",
        ),
        pytest.param(
            "/step/sync-endpoint", _INFO, Outcome.OK, {}, id="sync-form-in-a-sync-endpoint"
        ),
    ],
)
def test_a_step_writes_exactly_one_line_with_its_outcome_and_level(
    capsys: pytest.CaptureFixture[str],
    path: str,
    level: str,
    outcome: Outcome,
    extra: dict[str, str],
) -> None:
    _, lines = serve_requests(capsys, [get(path)])

    line = _step_line(lines)
    assert line["event"] == STEP_EVENT
    assert line["agent_id"] == STEP_AGENT_ID
    assert (line["level"], line["outcome"]) == (level, outcome.value)
    assert isinstance(line["duration_ms"], float)
    assert line["duration_ms"] >= 0
    assert {key: line[key] for key in extra} == extra
    assert "first_output_ms" not in line
    assert ("error_type" in line) == (outcome is Outcome.FAILED)
    assert ("reason" in line) == (outcome is Outcome.REFUSED)


@pytest.mark.parametrize(
    "path", ["/step/failed", "/step/failed-handled", "/step/failed-escaping", "/step/thread-failed"]
)
def test_a_failed_steps_line_carries_neither_the_message_nor_a_traceback(
    capsys: pytest.CaptureFixture[str], path: str
) -> None:
    _, lines = serve_requests(capsys, [get(path)])

    line = _step_line(lines)
    assert "exception" not in line
    assert STEP_FAILURE_MESSAGE not in str(line)
    assert "Traceback" not in str(line)


@pytest.mark.parametrize("path", ["/step/failed", "/step/failed-handled", "/step/thread-failed"])
def test_a_failed_steps_exception_reaches_the_caller_as_the_same_object(
    capsys: pytest.CaptureFixture[str], path: str
) -> None:
    _, _, (answer,) = serve_requests_answered(capsys, [get(path)])

    assert answer.json() == {"caught": STEP_FAILURE_MESSAGE, "same": True}


def test_a_cancelled_step_lets_the_cancellation_propagate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, (answer,) = serve_requests_answered(capsys, [get("/step/cancelled")])

    assert answer.json()["cancelled"] is True


def test_an_exception_that_leaves_the_step_unhandled_fails_the_request_with_one_step_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = serve_requests(capsys, [get("/step/failed-escaping")])

    assert _step_line(lines)["outcome"] == Outcome.FAILED.value
    (request,) = request_lines(lines)
    assert (request["status"], request["outcome"]) == (500, Outcome.FAILED.value)


def test_a_field_named_like_a_key_the_timer_owns_never_reaches_the_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = serve_requests(capsys, [get("/step/timer-named-fields")])

    line = _step_line(lines)
    assert line["outcome"] == Outcome.OK.value
    assert isinstance(line["duration_ms"], float)
    assert line["duration_ms"] >= 0
    assert not {"reason", "first_output_ms", "error_type"} & line.keys()


def test_the_first_output_mark_keeps_the_time_of_the_first_call_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = serve_requests(capsys, [get("/step/first-output")])

    line = _step_line(lines)
    assert isinstance(line["first_output_ms"], float)
    assert isinstance(line["duration_ms"], float)
    assert 0 <= line["first_output_ms"] <= line["duration_ms"]
    # A second mark taken after the delay would put first_output_ms at the end of the step.
    gap_ms = line["duration_ms"] - line["first_output_ms"]
    assert gap_ms >= STEP_SECOND_MARK_DELAY_SECONDS * 1000 * 0.8


@pytest.mark.parametrize(
    "path", ["/step/ok", "/step/thread", "/step/thread-failed", "/step/sync-endpoint"]
)
def test_a_step_line_carries_the_trace_of_the_request_it_ran_in(
    capsys: pytest.CaptureFixture[str], path: str
) -> None:
    _, lines = serve_requests(capsys, [get(path, traceparent=_TRACEPARENT)])

    assert _step_line(lines)["trace_id"] == _TRACE_ID
    (request,) = request_lines(lines)
    assert request["trace_id"] == _TRACE_ID


def test_a_step_line_is_written_through_the_logger_the_caller_names_without_configure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reads the logger directly through caplog, not through uvicorn and stdout.

    A host that never calls `configure` is the case under test, and `configure` would replace
    the host's own logging setup, so this is the one step test that does not serve a real app.
    """
    with caplog.at_level(logging.INFO, logger=STEP_LOGGER):
        with StepTimer(STEP_EVENT, logging.getLogger(STEP_LOGGER), agent_id=STEP_AGENT_ID):
            pass

    (record,) = caplog.records
    assert (record.name, record.getMessage(), record.levelno) == (
        STEP_LOGGER,
        STEP_EVENT,
        logging.INFO,
    )
    assert vars(record)["outcome"] == Outcome.OK.value
    assert vars(record)["agent_id"] == STEP_AGENT_ID


@pytest.mark.parametrize("field", ["name", "module", "process", "args", "message", "asctime"])
def test_a_field_named_like_a_log_record_attribute_is_refused_when_the_timer_is_built(
    field: str,
) -> None:
    with pytest.raises(ValueError, match=field):
        StepTimer(STEP_EVENT, logging.getLogger(STEP_LOGGER), **{field: "value"})
