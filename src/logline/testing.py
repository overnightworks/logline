"""For a host's tests: `configured` runs `configure` for the length of a block and undoes it.

Importing this module imports no test framework.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import structlog

from logline.configuration import TOUCHED_LOGGER_NAMES, configure

__all__ = ["configured"]


@dataclass(frozen=True, slots=True)
class _LoggerState:
    logger: logging.Logger
    handlers: list[logging.Handler]
    filters: list[Any]
    level: int
    propagate: bool

    @classmethod
    def of(cls, logger: logging.Logger) -> _LoggerState:
        return cls(logger, logger.handlers[:], logger.filters[:], logger.level, logger.propagate)

    def restore(self) -> None:
        self.logger.handlers[:] = self.handlers
        self.logger.filters[:] = self.filters
        self.logger.setLevel(self.level)
        self.logger.propagate = self.propagate


@contextmanager
def configured(service: str) -> Iterator[None]:
    """Call `configure(service)`, and on exit restore the logging state it changed.

    Restored are the handlers, filters, level and propagation of the root logger, uvicorn's, the
    HTTP clients' and the request logger, and structlog's configuration, as they were on entry.
    """
    logger_states = [_LoggerState.of(logging.getLogger(name)) for name in TOUCHED_LOGGER_NAMES]
    structlog_was_configured = structlog.is_configured()
    structlog_configuration = structlog.get_config()
    try:
        configure(service)
        yield
    finally:
        for state in logger_states:
            state.restore()
        if structlog_was_configured:
            structlog.configure(**structlog_configuration)
        else:
            structlog.reset_defaults()
