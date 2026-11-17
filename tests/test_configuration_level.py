import logging

import httpx
import pytest
import structlog
import uvicorn

from logline import configure
from tests.support.apps import build_app
from tests.support.lines import json_lines, request_lines
from tests.support.serving import serve

_ALL_LEVELS = ("debug", "info", "warning", "error", "critical")


def _emit_every_level_from_a_library() -> None:
    library = logging.getLogger("some_library")
    for level in _ALL_LEVELS:
        library.log(logging.getLevelNamesMapping()[level.upper()], "line at %s", level)


@pytest.mark.parametrize(
    ("setting", "appearing"),
    [
        (None, ("info", "warning", "error", "critical")),
        ("debug", _ALL_LEVELS),
        ("INFO", ("info", "warning", "error", "critical")),
        ("Warning", ("warning", "error", "critical")),
        ("error", ("error", "critical")),
        ("CRITICAL", ("critical",)),
    ],
)
def test_log_level_sets_which_lines_appear(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    setting: str | None,
    appearing: tuple[str, ...],
) -> None:
    if setting is not None:
        monkeypatch.setenv("LOG_LEVEL", setting)
    configure("levels")

    _emit_every_level_from_a_library()
    structlog.get_logger("tests").debug("structlog debug")

    lines = json_lines(capsys.readouterr().out)
    library_levels = tuple(line["level"] for line in lines if line["logger"] == "some_library")
    assert library_levels == appearing
    assert ("structlog debug" in {line["event"] for line in lines}) is (setting == "debug")


@pytest.mark.parametrize("setting", ["", "10", "warn", "fatal", "verbose", " info"])
def test_a_malformed_log_level_raises_naming_the_value_and_the_accepted_names(
    monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    monkeypatch.setenv("LOG_LEVEL", setting)

    with pytest.raises(ValueError) as raised:
        configure("levels")

    message = str(raised.value)
    assert repr(setting) in message
    for accepted in _ALL_LEVELS:
        assert accepted in message


def test_the_request_line_appears_at_the_error_level(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "error")
    configure("levels")

    async def exercise(base_url: str) -> None:
        async with httpx.AsyncClient(base_url=base_url) as client:
            await client.get("/items/1")

    serve(uvicorn.Config(build_app(), log_config=None), exercise)

    lines = json_lines(capsys.readouterr().out)
    assert [line["status"] for line in request_lines(lines)] == [200]
    assert {line["level"] for line in lines} == {"info"}


@pytest.mark.parametrize(
    ("setting", "logger", "emitted", "appears"),
    [
        ("error", "httpx", logging.WARNING, False),
        ("error", "mcp.client.streamable_http", logging.WARNING, False),
        ("error", "sqlalchemy.dialects.postgresql", logging.WARNING, False),
        ("critical", "piper", logging.ERROR, False),
        ("error", "httpx", logging.ERROR, True),
        ("debug", "httpx", logging.INFO, False),
        ("debug", "httpx", logging.WARNING, True),
        ("debug", "piper", logging.WARNING, False),
        ("debug", "piper", logging.ERROR, True),
    ],
)
def test_a_logger_cap_only_raises_the_floor_of_the_log_level(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    setting: str,
    logger: str,
    emitted: int,
    appears: bool,
) -> None:
    monkeypatch.setenv("LOG_LEVEL", setting)
    configure("levels")

    async def exercise(base_url: str) -> None:
        async with httpx.AsyncClient(base_url=base_url) as client:
            await client.get("/items/1")
        logging.getLogger(logger).log(emitted, "capped library line")

    serve(uvicorn.Config(build_app(), log_config=None), exercise)

    events = {line["event"] for line in json_lines(capsys.readouterr().out)}
    assert ("capped library line" in events) is appears
