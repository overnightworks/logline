import pytest

from logline import Outcome, RequestLine
from logline.trace import current_trace_id, request_trace
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
    ("path", "status", "outcome"),
    [
        pytest.param(OK_PATH, 200, Outcome.OK, id="ok"),
        pytest.param(REFUSED_PATH, 404, Outcome.OK, id="a-4xx-is-the-hosts-answer"),
        pytest.param(UNAVAILABLE_PATH, 503, Outcome.FAILED, id="a-5xx-fails"),
        pytest.param(RAISES_PATH, 500, Outcome.FAILED, id="an-exception-fails-with-500"),
        pytest.param(CANCELLED_PATH, 500, Outcome.CANCELLED, id="a-base-exception-cancels"),
        pytest.param(NO_STATUS_PATH, 500, Outcome.FAILED, id="no-status-set-reads-500"),
    ],
)
def test_a_threaded_host_writes_the_requests_outcome_and_status(
    capsys: pytest.CaptureFixture[str], path: str, status: int, outcome: Outcome
) -> None:
    _, lines = serve_threaded_requests(capsys, [get(path)])

    (line,) = request_lines(lines)
    assert (line["event"], line["outcome"], line["status"]) == (
        "request.ended",
        outcome.value,
        status,
    )


@pytest.mark.parametrize(
    ("sent", "logged"),
    [("GET", "GET"), ("DELETE", "DELETE"), ("BREW", "_OTHER")],
)
def test_a_threaded_hosts_method_is_a_standard_method_or_other(
    capsys: pytest.CaptureFixture[str], sent: str, logged: str
) -> None:
    _, lines = serve_threaded_requests(capsys, [Call(OK_PATH, method=sent)])

    assert request_lines(lines)[0]["method"] == logged


def test_a_threaded_host_reports_the_client_address_until_it_sets_another(
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


def test_the_failure_line_of_the_host_and_its_crash_line_follow_the_request_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, lines = serve_threaded_requests(
        capsys,
        [
            Call(RAISES_PATH, headers={"traceparent": _INCOMING_TRACEPARENT}),
            get(CANCELLED_PATH),
        ],
    )

    (failed,) = [line for line in lines_of(lines, HOST_LOGGER)]
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


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([("TraceParent", _INCOMING_TRACEPARENT)], id="text-pairs-any-case"),
        pytest.param([(b"traceparent", _INCOMING_TRACEPARENT.encode())], id="asgi-byte-pairs"),
    ],
)
def test_the_trace_is_read_from_text_and_byte_header_pairs(
    headers: list[tuple[str, str]] | list[tuple[bytes, bytes]],
) -> None:
    with request_trace(headers):
        assert current_trace_id() == _INCOMING_TRACE_ID


def test_a_request_line_reads_its_headers_case_insensitively() -> None:
    with RequestLine("GET", [("TRACEPARENT", _INCOMING_TRACEPARENT)]):
        assert current_trace_id() == _INCOMING_TRACE_ID


def _pairs(lines: list[dict[str, object]]) -> list[tuple[dict[str, object], dict[str, object]]]:
    inside = lines_of(lines, HOST_LOGGER)
    return list(zip(inside, request_lines(lines), strict=True))
