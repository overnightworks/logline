import logging
from dataclasses import dataclass

import pytest
import structlog

from logline import configure
from logline.testing import configured
from tests.support.lines import json_lines
from tests.support.loggers import logger_settings


@dataclass(frozen=True)
class _Seen:
    """What a host can observe of the process's logging."""

    loggers: dict[str, tuple[int, bool, tuple[object, ...], tuple[object, ...]]]
    structlog_is_configured: bool
    structlog_processors: list[object]


def _seen() -> _Seen:
    return _Seen(
        loggers=logger_settings(),
        structlog_is_configured=structlog.is_configured(),
        structlog_processors=list(structlog.get_config()["processors"]),
    )


def _leave_logging_in_a_host_state() -> None:
    uvicorn_error = logging.getLogger("uvicorn.error")
    uvicorn_error.addHandler(logging.NullHandler())
    uvicorn_error.addFilter(lambda record: True)
    uvicorn_error.setLevel(logging.ERROR)
    uvicorn_error.propagate = False
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger().setLevel(logging.DEBUG)


def test_lines_inside_the_block_are_json(capsys: pytest.CaptureFixture[str]) -> None:
    with configured("inside"):
        logging.getLogger("some_library").warning("a line")

    (line,) = json_lines(capsys.readouterr().out)
    assert (line["service"], line["event"]) == ("inside", "a line")


@pytest.mark.parametrize("structlog_configured_before", [False, True])
def test_leaving_the_block_restores_the_logging_the_host_had(
    structlog_configured_before: bool,
) -> None:
    _leave_logging_in_a_host_state()
    if structlog_configured_before:
        structlog.configure(processors=[structlog.processors.add_log_level])
    before = _seen()

    with configured("restoring"):
        assert _seen() != before

    assert _seen() == before


def test_leaving_the_block_restores_the_logger_class_the_host_had() -> None:
    class HostLogger(logging.Logger):
        pass

    original_class = logging.getLoggerClass()
    logging.setLoggerClass(HostLogger)
    try:
        with configured("logger-class"):
            pass

        assert logging.getLoggerClass() is HostLogger
    finally:
        logging.setLoggerClass(original_class)


def test_a_failing_block_still_restores_the_logging() -> None:
    before = _seen()

    with pytest.raises(ZeroDivisionError), configured("failing"):
        raise ZeroDivisionError

    assert _seen() == before


def test_blocks_in_a_row_each_restore_and_no_handler_accumulates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _seen()

    with configured("first"):
        logging.getLogger("some_library").warning("in the first")
    with configured("second"):
        logging.getLogger("some_library").warning("in the second")

    assert _seen() == before
    assert [line["event"] for line in json_lines(capsys.readouterr().out)] == [
        "in the first",
        "in the second",
    ]


def test_a_block_inside_a_process_configured_earlier_restores_that_configuration(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("outer")
    with configured("inner"):
        logging.getLogger("some_library").warning("inner line")
    logging.getLogger("some_library").warning("outer line")

    services = {line["event"]: line["service"] for line in json_lines(capsys.readouterr().out)}
    assert services == {"inner line": "inner", "outer line": "outer"}
