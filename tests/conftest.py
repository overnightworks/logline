import logging
from collections.abc import Iterator

import pytest
import structlog
from structlog.stdlib import ProcessorFormatter

from logline.configuration import TOUCHED_LOGGER_NAMES

pytest.register_assert_rewrite("tests.support")


@pytest.fixture(autouse=True)
def isolated_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give each test the process's logging as it was, and no logline environment."""
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("SERVICE_VERSION", raising=False)
    loggers = [logging.getLogger(name) for name in TOUCHED_LOGGER_NAMES]
    saved = [
        (logger, logger.handlers[:], logger.filters[:], logger.level, logger.propagate)
        for logger in loggers
    ]
    yield
    for logger, handlers, filters, level, propagate in saved:
        # The root's handlers belong to pytest's capture; only the ones `configure` added go.
        if logger.name == "root":
            for handler in logger.handlers[:]:
                if isinstance(handler.formatter, ProcessorFormatter):
                    logger.removeHandler(handler)
        else:
            logger.handlers[:] = handlers
        logger.filters[:] = filters
        logger.setLevel(level)
        logger.propagate = propagate
    structlog.reset_defaults()
