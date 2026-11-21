import pytest

from logline import REQUEST_EVENT, Outcome, RequestLine
from logline.trace import current_trace_id
from tests.support.apps import HOST_CLIENT_IP, HOST_ROUTE, SUB
from tests.support.lines import lines_of, request_lines
from tests.support.serving import Call, get
from tests.support.threaded_host import (
    CANCELLED_PATH,
    ENDED_EARLY_PATH,
    HANDLE_ERROR_EVENT,
    HANDLED_EVENT,
    HOST_LOGGER,
    IDENTITY_PATH,
    LOGGING_PATH,
    NO_STATUS_PATH,
    OK_PATH,
    RAISES_PATH,
    REFUSED_PATH,
    UNAVAILABLE_PATH,
    serve_threaded_requests,
)

_INCOMING_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
_INCOMING_SPAN_ID = "00f067aa0ba902b7"
_INCOMING_TRACEPARENT = f"00-{_INCOMING_TRACE_ID}-{_INCOMING_SPAN_ID}-01"


@pytest.mark.parametrize(
    ("path", "sent", "method", "status", "outcome"),
    [
        pytest.param(OK_PATH, "GET", "GET", 200, Outcome.OK, id="ok"),
        pytest.param(OK_PATH, "DELETE", "DELETE", 200, Outcome.OK, id="a-standard-method"),
        pytest.param(OK_PATH, "BREW", "_OTHER", 200, Outcome.OK, id="another-method-is-other"),
        pytest.param(REFUSED_PATH, "GET", "GET", 404, Outcome.OK, id="a-4xx-is-the-hosts-answer"),
        pytest.param(UNAVAILABLE_PATH, "GET", "GET", 503, Outcome.FAILED, id="a-5xx-fails"),
        pytest.param(RAISES_PATH, "GET", "GET", 500, Outcome.FAILED, id="an-exception-fails"),
        pytest.param(CANCELLED_PATH, "GET", "GET", 500, Outcome.CANCELLED, id="a-base-exception"),
        pytest.param(NO_STATUS_PATH, "GET", "GET", 500, Outcome.FAILED, id="no-status-set"),
    ],
)
def test_a_threaded_host_writes_the_requests_method_outcome_and_status(
    capsys: pytest.CaptureFixture[str],
    path: str,
    sent: str,
    method: str,
    status: int,
    outcome: Outcome,
) -> None:
    _, lines = serve_threaded_requests(capsys, [Call(path, method=sent)])

    (line,) = request_lines(lines)
    assert (line["event"], line["method"], line["outcome"], line["status"]) == (
        REQUEST_EVENT,
        method,
        outcome.value,
        status,
    )


def test_a_threaded_host_reports_no_sub_or_route_and_the_client_address_until_it_sets_them(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = serve_threaded_requests(capsys, [get(OK_PATH), get(IDENTITY_PATH)])

    plain, identified = request_lines(lines)
    assert (plain["sub"], plain["ip"], plain["route"]) == (None, "127.0.0.1", None)
    assert (identified["sub"], identified["ip"], identified["route"]) == (
        SUB,
        HOST_CLIENT_IP,
        HOST_ROUTE,
    )


def test_a_threaded_host_continues_the_incoming_trace_on_every_line_of_the_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = serve_threaded_requests(
        capsys, [Call(LOGGING_PATH, headers={"traceparent": _INCOMING_TRACEPARENT})]
    )

    (inside,) = lines_of(lines, HOST_LOGGER)
    (line,) = request_lines(lines)
    assert inside["event"] == HANDLED_EVENT
    assert {inside["trace_id"], line["trace_id"]} == {_INCOMING_TRACE_ID}


def test_a_threaded_host_starts_a_new_trace_for_a_request_without_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = serve_threaded_requests(capsys, [get(LOGGING_PATH), get(LOGGING_PATH)])

    traces = [(inside["trace_id"], line["trace_id"]) for inside, line in _pairs(lines)]
    assert all(inside == line and inside is not None for inside, line in traces)
    assert traces[0][0] != traces[1][0]


def test_the_hosts_failure_line_joins_the_trace_and_a_base_exception_leaves_a_crash_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = serve_threaded_requests(
        capsys,
        [
            Call(RAISES_PATH, headers={"traceparent": _INCOMING_TRACEPARENT}),
            get(CANCELLED_PATH),
        ],
    )

    (failed,) = lines_of(lines, HOST_LOGGER)
    assert (failed["event"], failed["trace_id"]) == (HANDLE_ERROR_EVENT, _INCOMING_TRACE_ID)
    (crashed,) = [line for line in lines if line["event"] == "thread.crashed"]
    assert crashed["error_type"] == "HostCancelled"
    assert [line["outcome"] for line in request_lines(lines)] == ["failed", "cancelled"]


def test_ending_the_line_before_the_block_ends_writes_it_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = serve_threaded_requests(capsys, [get(ENDED_EARLY_PATH)])

    (line,) = request_lines(lines)
    assert (line["outcome"], line["status"]) == ("ok", 200)


def test_a_request_line_reads_its_headers_case_insensitively() -> None:
    with RequestLine("GET", [("TRACEPARENT", _INCOMING_TRACEPARENT)]):
        assert current_trace_id() == _INCOMING_TRACE_ID


def _pairs(lines: list[dict[str, object]]) -> list[tuple[dict[str, object], dict[str, object]]]:
    inside = lines_of(lines, HOST_LOGGER)
    return list(zip(inside, request_lines(lines), strict=True))


@pytest.mark.usefixtures("host_with_otel_propagators_none")
def test_a_threaded_host_follows_the_global_propagator_when_otel_propagators_is_set(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = serve_threaded_requests(
        capsys, [Call(LOGGING_PATH, headers={"traceparent": _INCOMING_TRACEPARENT})]
    )

    (line,) = request_lines(lines)
    assert line["trace_id"] not in {None, _INCOMING_TRACE_ID}
