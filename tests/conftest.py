import logging
import sys
import threading
import warnings
from collections.abc import Iterator
from dataclasses import replace

import pytest
import structlog
from structlog.stdlib import ProcessorFormatter

from logline.configuration import TOUCHED_LOGGER_NAMES
from logline.testing import LoggerState

pytest.register_assert_rewrite("tests.support")


@pytest.fixture(autouse=True)
def isolated_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give each test the process's logging as it was, and no logline environment."""
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("SERVICE_VERSION", raising=False)
    hooks = (sys.excepthook, threading.excepthook, warnings.showwarning)
    saved = [LoggerState.of(logging.getLogger(name)) for name in TOUCHED_LOGGER_NAMES]
    yield
    sys.excepthook, threading.excepthook, warnings.showwarning = hooks
    for state in saved:
        if state.logger is logging.getLogger():
            # The root's handlers belong to pytest's capture, which swaps them between the phases of
            # a test, so restoring the snapshot would bring back the setup phase's; only the ones
            # `configure` added go.
            for handler in state.logger.handlers[:]:
                if isinstance(handler.formatter, ProcessorFormatter):
                    state.logger.removeHandler(handler)
            state = replace(state, handlers=state.logger.handlers[:])
        state.restore()
    structlog.reset_defaults()
