import re

import httpx
import pytest
import structlog
import uvicorn

from logline import configure, outgoing_headers
from tests.support.apps import build_app
from tests.support.lines import json_lines, request_lines
from tests.support.serving import serve

_TRACEPARENT = re.compile(
    r"^00-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)
_INCOMING_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
_INCOMING_SPAN_ID = "00f067aa0ba902b7"
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
    match = _TRACEPARENT.match(body["headers"]["traceparent"])
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
    traceparent = f"00-{_INCOMING_TRACE_ID}-{_INCOMING_SPAN_ID}-01"

    body = _call(path, traceparent=traceparent, tracestate=_INCOMING_TRACESTATE)

    lines = json_lines(capsys.readouterr().out)
    in_request = [line for line in lines if line["logger"] != "uvicorn.error"]
    assert len(in_request) == lines_in_the_request
    assert {line["trace_id"] for line in in_request} == {_INCOMING_TRACE_ID}
    outgoing = _traceparent(body)
    assert outgoing["trace_id"] == _INCOMING_TRACE_ID
    assert outgoing["span_id"] != _INCOMING_SPAN_ID
    assert outgoing["flags"] == "01"
    assert body["headers"]["tracestate"] == _INCOMING_TRACESTATE


@pytest.mark.parametrize(
    "incoming",
    [
        pytest.param({}, id="absent"),
        pytest.param({"traceparent": "garbage"}, id="malformed"),
        pytest.param(
            {"traceparent": f"00-{'0' * 32}-{_INCOMING_SPAN_ID}-01"}, id="all-zero trace id"
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
