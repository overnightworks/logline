import logging
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
import structlog
import uvicorn

from logline import configure
from tests.support.apps import build_app
from tests.support.lines import BASE_KEYS, Line, json_lines
from tests.support.serving import serve

_LOCAL_SECRET = "local-secret-value"
_MESSAGE = "visible message"
_CAUSE_MESSAGE = "inner cause message"


def _logged_lines(capsys: pytest.CaptureFixture[str]) -> list[Line]:
    return json_lines(capsys.readouterr().out)


def test_every_line_of_a_served_request_is_json_in_the_record_shape(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("shape-service")

    async def exercise(base_url: str) -> None:
        async with httpx.AsyncClient(base_url=base_url) as client:
            await client.get("/library-line")

    serve(uvicorn.Config(build_app(), log_config=None), exercise)

    lines = _logged_lines(capsys)
    loggers = {line["logger"] for line in lines}
    assert {"uvicorn.error", "some_library", "logline.request"} <= loggers
    for line in lines:
        assert BASE_KEYS <= line.keys()
        assert line["service"] == "shape-service"
        assert line["level"] in {"debug", "info", "warning", "error", "critical"}
        timestamp = datetime.fromisoformat(str(line["ts"]))
        assert timestamp.utcoffset() == UTC.utcoffset(None)


@pytest.mark.parametrize(
    ("environment", "expected_version"),
    [({}, None), ({"SERVICE_VERSION": "2026.11.14.1"}, "2026.11.14.1")],
)
def test_version_comes_from_service_version_or_is_null(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environment: dict[str, str],
    expected_version: str | None,
) -> None:
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    configure("versioned")

    structlog.get_logger("tests").info("a line")

    (line,) = _logged_lines(capsys)
    assert line["version"] == expected_version


def test_a_line_outside_a_request_has_a_null_trace_id(capsys: pytest.CaptureFixture[str]) -> None:
    configure("outside")

    structlog.get_logger("tests").info("not in a request")

    (line,) = _logged_lines(capsys)
    assert line["trace_id"] is None


def test_a_line_adds_only_its_key_value_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure("fields")

    structlog.get_logger("tests").info("order placed", order_id="o-17", items=3)

    (line,) = _logged_lines(capsys)
    assert line["event"] == "order placed"
    assert line["order_id"] == "o-17"
    assert line["items"] == 3
    assert line.keys() - BASE_KEYS == {"order_id", "items"}


def test_a_caller_cannot_overwrite_a_base_field(capsys: pytest.CaptureFixture[str]) -> None:
    configure("honest")

    structlog.get_logger("tests").info("spoof", service="someone-else", level="critical")

    (line,) = _logged_lines(capsys)
    assert line["service"] == "honest"
    assert line["level"] == "info"


def test_a_second_configure_does_not_duplicate_lines(capsys: pytest.CaptureFixture[str]) -> None:
    configure("twice")
    configure("twice")

    logging.getLogger("some_library").warning("once")

    assert len(_logged_lines(capsys)) == 1


@pytest.mark.parametrize(
    "emit",
    [
        pytest.param(lambda: structlog.get_logger("tests").exception("failed"), id="structlog"),
        pytest.param(lambda: logging.getLogger("tests").exception("failed"), id="stdlib"),
    ],
)
def test_an_exception_renders_as_text_with_its_message_and_without_its_locals(
    capsys: pytest.CaptureFixture[str], emit: Callable[[], None]
) -> None:
    configure("exceptions")

    def raise_holding_a_secret() -> None:
        secret_in_a_local = _LOCAL_SECRET
        raise RuntimeError(f"{_MESSAGE} after {len(secret_in_a_local)} characters")

    try:
        raise_holding_a_secret()
    except RuntimeError:
        emit()

    output = capsys.readouterr().out
    (line,) = json_lines(output)
    assert isinstance(line["exception"], str)
    assert f"RuntimeError: {_MESSAGE} after 18 characters" in line["exception"]
    assert "raise_holding_a_secret" in line["exception"]
    assert _LOCAL_SECRET not in output


def _raise_with_a_cause() -> None:
    try:
        raise KeyError(_CAUSE_MESSAGE)
    except KeyError as cause:
        raise ValueError(_MESSAGE) from cause


def _raise_during_handling() -> None:
    try:
        raise KeyError(_CAUSE_MESSAGE)
    except KeyError:
        raise ValueError(_MESSAGE)  # noqa: B904 - the implicit context is the subject


def _raise_with_the_context_suppressed() -> None:
    try:
        raise KeyError(_CAUSE_MESSAGE)
    except KeyError:
        raise ValueError(_MESSAGE) from None


def _raise_in_a_cause_cycle() -> None:
    first = ValueError(_MESSAGE)
    second = KeyError(_CAUSE_MESSAGE)
    first.__cause__ = second
    second.__cause__ = first
    raise first


def _raise_in_a_context_cycle() -> None:
    first = ValueError(_MESSAGE)
    second = KeyError(_CAUSE_MESSAGE)
    first.__context__ = second
    second.__context__ = first
    raise first


def _raise_a_group() -> None:
    raise ExceptionGroup("group message", [ValueError(_MESSAGE), TypeError(_CAUSE_MESSAGE)])


@pytest.mark.parametrize(
    ("raiser", "rendered_in_order"),
    [
        pytest.param(
            _raise_with_a_cause,
            [
                f"KeyError: {_CAUSE_MESSAGE!r}",
                "direct cause of the following",
                f"ValueError: {_MESSAGE}",
            ],
            id="cause",
        ),
        pytest.param(
            _raise_during_handling,
            [
                f"KeyError: {_CAUSE_MESSAGE!r}",
                "another exception occurred",
                f"ValueError: {_MESSAGE}",
            ],
            id="context",
        ),
        pytest.param(
            _raise_with_the_context_suppressed, [f"ValueError: {_MESSAGE}"], id="suppressed"
        ),
        pytest.param(
            _raise_in_a_cause_cycle,
            [
                f"KeyError: {_CAUSE_MESSAGE!r}",
                "direct cause of the following",
                f"ValueError: {_MESSAGE}",
            ],
            id="cause-cycle",
        ),
        pytest.param(
            _raise_in_a_context_cycle,
            [
                f"KeyError: {_CAUSE_MESSAGE!r}",
                "another exception occurred",
                f"ValueError: {_MESSAGE}",
            ],
            id="context-cycle",
        ),
        pytest.param(
            _raise_a_group,
            [
                "ExceptionGroup: group message",
                f"ValueError: {_MESSAGE}",
                f"TypeError: {_CAUSE_MESSAGE}",
            ],
            id="group",
        ),
    ],
)
def test_a_chained_exception_renders_every_type_and_message_in_order(
    capsys: pytest.CaptureFixture[str],
    raiser: Callable[[], None],
    rendered_in_order: list[str],
) -> None:
    configure("exceptions")

    try:
        raiser()
    except ValueError, ExceptionGroup:
        structlog.get_logger("tests").exception("failed")

    (line,) = json_lines(capsys.readouterr().out)
    text = str(line["exception"])
    positions = [text.index(part) for part in rendered_in_order]
    assert positions == sorted(positions)
    assert ("KeyError" in text) == any("KeyError" in part for part in rendered_in_order)
