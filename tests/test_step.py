import logging

import pytest

from logline import Outcome, StepTimer, configure
from logline.trace import current_trace_id
from tests.support.incoming_trace import INCOMING_TRACE_ID, INCOMING_TRACEPARENT
from tests.support.lines import Line, json_lines, lines_of, request_lines
from tests.support.serving import get, serve_requests, serve_requests_answered
from tests.support.step_routes import (
    STEP,
    STEP_AGENT_ID,
    STEP_EVENT,
    STEP_FAILURE_MESSAGE,
    STEP_LOGGER,
    STEP_REFUSAL_REASON,
)

_INFO, _WARNING, _ERROR = "info", "warning", "error"


def _ms(seconds: int) -> int:
    return seconds * 1000


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
        pytest.param(
            "/step/fail",
            _ERROR,
            Outcome.FAILED,
            {"reason": STEP.failure_reason},
            id="fail-without-an-exception",
        ),
        pytest.param(
            "/step/fail-handled",
            _WARNING,
            Outcome.FAILED,
            {"reason": STEP.failure_reason},
            id="fail-without-an-exception-and-its-unit-goes-on",
        ),
        pytest.param(
            "/step/ok-with-reason",
            _INFO,
            Outcome.OK,
            {"reason": STEP.ok_reason},
            id="ok-with-a-reason",
        ),
        pytest.param(
            "/step/reason-then-refuse",
            _INFO,
            Outcome.REFUSED,
            {"reason": STEP_REFUSAL_REASON},
            id="refuse-after-reason-replaces-it",
        ),
        pytest.param(
            "/step/reason-then-fail",
            _ERROR,
            Outcome.FAILED,
            {"reason": STEP.failure_reason},
            id="fail-after-reason-replaces-it",
        ),
        pytest.param(
            "/step/reason-then-raise",
            _ERROR,
            Outcome.FAILED,
            {"error_type": "ValueError"},
            id="exception-after-reason-fails-without-it",
        ),
        pytest.param(
            "/step/reason-then-cancel",
            _INFO,
            Outcome.CANCELLED,
            {},
            id="cancellation-after-reason-carries-none",
        ),
        pytest.param(
            "/step/fail-then-refuse",
            _ERROR,
            Outcome.FAILED,
            {"reason": STEP.failure_reason},
            id="refuse-after-fail-changes-nothing",
        ),
        pytest.param(
            "/step/refuse-then-fail",
            _ERROR,
            Outcome.FAILED,
            {"reason": STEP.failure_reason},
            id="fail-after-refuse-fails-the-step",
        ),
        pytest.param(
            "/step/degraded-then-fail",
            _ERROR,
            Outcome.FAILED,
            {"reason": STEP.failure_reason},
            id="a-degraded-step-that-fails-stays-at-error",
        ),
        pytest.param(
            "/step/add-reserved",
            _ERROR,
            Outcome.FAILED,
            {"error_type": "ValueError"},
            id="a-record-attribute-name-in-add-fails-the-block",
        ),
        pytest.param(
            "/step/add-reserved-escaping",
            _ERROR,
            Outcome.FAILED,
            {"error_type": "ValueError"},
            id="a-record-attribute-name-in-add-fails-the-block-it-escapes",
        ),
        pytest.param(
            "/step/refuse-then-raise",
            _ERROR,
            Outcome.FAILED,
            {"error_type": "ValueError"},
            id="exception-after-refuse-fails-without-the-refusal-reason",
        ),
        pytest.param(
            "/step/fail-then-raise",
            _ERROR,
            Outcome.FAILED,
            {"reason": STEP.failure_reason, "error_type": "ValueError"},
            id="exception-after-fail-carries-both",
        ),
        pytest.param(
            "/step/fail-then-cancel", _INFO, Outcome.CANCELLED, {}, id="cancellation-after-fail"
        ),
        pytest.param(
            "/step/add",
            _INFO,
            Outcome.OK,
            {
                "input_tokens": STEP.count,
                "output_tokens": STEP.count,
                "agent_id": STEP.later_agent_id,
            },
            id="add-puts-counts-on-the-line-and-a-later-value-wins",
        ),
        pytest.param(
            "/step/end",
            _INFO,
            Outcome.OK,
            {"output_tokens": STEP.count},
            id="end-writes-the-line-of-a-step-without-a-block",
        ),
        pytest.param(
            "/step/fail-then-end",
            _ERROR,
            Outcome.FAILED,
            {"reason": STEP.failure_reason},
            id="end-after-fail-writes-the-failure",
        ),
        pytest.param(
            "/step/refuse-then-end",
            _INFO,
            Outcome.REFUSED,
            {"reason": STEP_REFUSAL_REASON},
            id="end-after-refuse-writes-the-refusal",
        ),
        pytest.param("/step/sync-end", _INFO, Outcome.OK, {}, id="end-in-a-sync-endpoint"),
        pytest.param("/step/end-twice", _INFO, Outcome.OK, {}, id="a-second-end-writes-none"),
        pytest.param(
            "/step/end-in-block", _INFO, Outcome.OK, {}, id="the-exit-after-end-writes-none"
        ),
        pytest.param(
            "/step/end-then-raise",
            _INFO,
            Outcome.OK,
            {},
            id="an-exception-after-end-writes-no-second-line",
        ),
        pytest.param(
            "/step/end-cancelled",
            _INFO,
            Outcome.CANCELLED,
            {"output_tokens": STEP.count},
            id="end-cancelled-writes-the-line-of-a-step-without-a-block",
        ),
        pytest.param(
            "/step/sync-end-cancelled",
            _INFO,
            Outcome.CANCELLED,
            {},
            id="end-cancelled-in-a-sync-endpoint",
        ),
        pytest.param(
            "/step/fail-then-end-cancelled",
            _INFO,
            Outcome.CANCELLED,
            {},
            id="end-cancelled-after-fail-carries-no-reason",
        ),
        pytest.param(
            "/step/refuse-then-end-cancelled",
            _INFO,
            Outcome.CANCELLED,
            {},
            id="end-cancelled-after-refuse-carries-no-reason",
        ),
        pytest.param(
            "/step/handled-then-end-cancelled",
            _INFO,
            Outcome.CANCELLED,
            {},
            id="end-cancelled-after-handled-stays-at-info",
        ),
        pytest.param(
            "/step/degraded-then-end-cancelled",
            _WARNING,
            Outcome.CANCELLED,
            {},
            id="end-cancelled-after-degraded-is-a-warning",
        ),
        pytest.param(
            "/step/end-cancelled-then-raise",
            _INFO,
            Outcome.CANCELLED,
            {},
            id="an-exception-after-end-cancelled-writes-no-second-line",
        ),
        pytest.param(
            "/step/end-cancelled-then-end",
            _INFO,
            Outcome.CANCELLED,
            {},
            id="end-after-end-cancelled-writes-none",
        ),
        pytest.param(
            "/step/end-then-end-cancelled",
            _INFO,
            Outcome.OK,
            {},
            id="end-cancelled-after-end-writes-none",
        ),
        pytest.param(
            "/step/end-cancelled-twice",
            _INFO,
            Outcome.CANCELLED,
            {},
            id="a-second-end-cancelled-writes-none",
        ),
        pytest.param(
            "/step/end-cancelled-in-block",
            _INFO,
            Outcome.CANCELLED,
            {},
            id="the-exit-after-end-cancelled-writes-none",
        ),
        pytest.param(
            "/step/end-failed",
            _ERROR,
            Outcome.FAILED,
            {"output_tokens": STEP.count, "error_type": "ValueError"},
            id="end-failed-writes-the-failure-of-a-step-without-a-block",
        ),
        pytest.param(
            "/step/sync-end-failed",
            _ERROR,
            Outcome.FAILED,
            {"error_type": "ValueError"},
            id="end-failed-in-a-sync-endpoint",
        ),
        pytest.param(
            "/step/fail-then-end-failed",
            _ERROR,
            Outcome.FAILED,
            {"reason": STEP.failure_reason, "error_type": "ValueError"},
            id="end-failed-after-fail-carries-both",
        ),
        pytest.param(
            "/step/handled-then-end-failed",
            _WARNING,
            Outcome.FAILED,
            {"error_type": "ValueError"},
            id="end-failed-after-handled-is-a-warning",
        ),
        pytest.param(
            "/step/degraded-then-end-failed",
            _ERROR,
            Outcome.FAILED,
            {"error_type": "ValueError"},
            id="end-failed-after-degraded-stays-an-error",
        ),
        pytest.param(
            "/step/end-failed-then-end",
            _ERROR,
            Outcome.FAILED,
            {"error_type": "ValueError"},
            id="end-after-end-failed-writes-none",
        ),
        pytest.param(
            "/step/end-then-end-failed",
            _INFO,
            Outcome.OK,
            {},
            id="end-failed-after-end-writes-none",
        ),
        pytest.param(
            "/step/end-failed-then-end-cancelled",
            _ERROR,
            Outcome.FAILED,
            {"error_type": "ValueError"},
            id="end-cancelled-after-end-failed-writes-none",
        ),
        pytest.param(
            "/step/end-cancelled-then-end-failed",
            _INFO,
            Outcome.CANCELLED,
            {},
            id="end-failed-after-end-cancelled-writes-none",
        ),
        pytest.param(
            "/step/end-failed-twice",
            _ERROR,
            Outcome.FAILED,
            {"error_type": "ValueError"},
            id="a-second-end-failed-writes-none",
        ),
        pytest.param(
            "/step/end-failed-in-block",
            _ERROR,
            Outcome.FAILED,
            {"error_type": "ValueError"},
            id="the-exit-after-end-failed-writes-none",
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
    assert line["agent_id"] == extra.get("agent_id", STEP_AGENT_ID)
    assert (line["level"], line["outcome"]) == (level, outcome.value)
    assert isinstance(line["duration_ms"], float)
    assert line["duration_ms"] >= 0
    assert {key: line[key] for key in extra} == extra
    assert "first_output_ms" not in line
    assert ("error_type" in line) == ("error_type" in extra)
    assert ("reason" in line) == ("reason" in extra)
    assert "started_at" not in line


@pytest.mark.parametrize(
    "path",
    [
        "/step/failed",
        "/step/failed-handled",
        "/step/failed-escaping",
        "/step/thread-failed",
        "/step/fail-then-raise",
        "/step/refuse-then-raise",
        "/step/end-failed",
    ],
)
def test_a_failed_steps_line_carries_neither_the_message_nor_a_traceback(
    capsys: pytest.CaptureFixture[str], path: str
) -> None:
    _, lines = serve_requests(capsys, [get(path)])

    line = _step_line(lines)
    assert "exception" not in line
    assert STEP_FAILURE_MESSAGE not in str(line)
    assert "Traceback" not in str(line)


@pytest.mark.parametrize(
    "path",
    [
        "/step/failed",
        "/step/failed-handled",
        "/step/thread-failed",
        "/step/fail-then-raise",
        "/step/refuse-then-raise",
        "/step/end-then-raise",
        "/step/end-cancelled-then-raise",
    ],
)
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


@pytest.mark.parametrize("path", ["/step/failed-escaping", "/step/add-reserved-escaping"])
def test_an_exception_that_leaves_the_step_unhandled_fails_the_request_with_one_step_line(
    capsys: pytest.CaptureFixture[str], path: str
) -> None:
    _, lines = serve_requests(capsys, [get(path)])

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


def test_a_field_added_under_a_log_record_attribute_name_raises_out_of_the_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, (answer,) = serve_requests_answered(capsys, [get("/step/add-reserved")])

    assert STEP.reserved_field in str(answer.json()["caught"])


def test_a_step_counts_from_its_construction_not_from_the_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = serve_requests(capsys, [get("/step/constructed-before-the-block")])

    line = _step_line(lines)
    assert line["first_output_ms"] == _ms(STEP.seconds_before_first_output)
    assert line["duration_ms"] == _ms(
        STEP.seconds_before_first_output + STEP.seconds_after_first_output
    )


def test_the_first_output_mark_keeps_the_time_of_the_first_call_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = serve_requests(capsys, [get("/step/first-output")])

    line = _step_line(lines)
    assert line["first_output_ms"] == _ms(STEP.seconds_before_first_output)
    assert line["duration_ms"] == _ms(
        STEP.seconds_before_first_output + STEP.seconds_after_first_output
    )


def test_a_step_given_a_past_start_counts_its_durations_from_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = serve_requests(capsys, [get("/step/started-earlier")])

    line = _step_line(lines)
    assert line["first_output_ms"] == _ms(STEP.earlier_start_seconds)
    assert line["duration_ms"] == _ms(STEP.earlier_start_seconds + STEP.seconds_after_first_output)
    assert "started_at" not in line


def test_a_step_given_a_start_in_the_future_raises_and_writes_no_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines, (answer,) = serve_requests_answered(capsys, [get("/step/started-in-the-future")])

    assert "started_at" in str(answer.json()["caught"])
    assert lines_of(lines, STEP_LOGGER) == []


@pytest.mark.parametrize(
    "path",
    [
        "/step/ok",
        "/step/thread",
        "/step/thread-failed",
        "/step/sync-endpoint",
        "/step/end",
        "/step/sync-end",
        "/step/end-cancelled",
        "/step/sync-end-cancelled",
        "/step/end-failed",
        "/step/sync-end-failed",
    ],
)
def test_a_step_line_carries_the_trace_of_the_request_it_ran_in(
    capsys: pytest.CaptureFixture[str], path: str
) -> None:
    _, lines = serve_requests(capsys, [get(path, traceparent=INCOMING_TRACEPARENT)])

    assert _step_line(lines)["trace_id"] == INCOMING_TRACE_ID
    (request,) = request_lines(lines)
    assert request["trace_id"] == INCOMING_TRACE_ID


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


@pytest.mark.parametrize("enters_a_block", [True, False], ids=["block", "end"])
def test_a_step_outside_any_trace_writes_null_span_ids_and_starts_no_trace(
    capsys: pytest.CaptureFixture[str], enters_a_block: bool
) -> None:
    configure("step-service")
    step = StepTimer(STEP_EVENT, logging.getLogger(STEP_LOGGER))

    if enters_a_block:
        with step:
            assert current_trace_id() is None
    else:
        step.end()

    (line,) = json_lines(capsys.readouterr().out)
    assert (line["span_id"], line["parent_span_id"], line["trace_id"]) == (None, None, None)
    assert current_trace_id() is None


@pytest.mark.parametrize("field", ["name", "module", "process", "args", "message", "asctime"])
def test_a_field_named_like_a_log_record_attribute_is_refused_when_the_timer_is_built(
    field: str,
) -> None:
    with pytest.raises(ValueError, match=field):
        StepTimer(STEP_EVENT, logging.getLogger(STEP_LOGGER), **{field: 1})
