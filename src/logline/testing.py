"""For a host's tests: `configured` runs `configure` for the length of a block and undoes it.

Importing this module imports no test framework.
"""

import logging
import sys
import threading
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from opentelemetry.propagate import get_global_textmap, set_global_textmap

from logline import asyncio_records
from logline.configuration import TOUCHED_LOGGER_NAMES, configure
from logline.outgoing_trace import is_enabled, set_enabled

if TYPE_CHECKING:
    from logging import _FilterType

__all__ = ["configured"]


@dataclass(frozen=True, slots=True)
class LoggerState:
    logger: logging.Logger
    handlers: list[logging.Handler]
    filters: list[_FilterType]
    level: int
    propagate: bool

    @classmethod
    def of(cls, logger: logging.Logger) -> LoggerState:
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
    HTTP clients', the request and the capped library loggers, structlog's configuration and the
    stdlib logger class structlog's factory installs, the process's and the threads' exception
    hooks, the unraisable-exception hook and the warning display, OpenTelemetry's global propagator
    and whether outgoing requests carry the trace, as they were on entry. The carriers stay
    installed on the HTTP clients and their methods are never reassigned, so a wrapper a host put on
    one survives.
    """
    logger_states = [LoggerState.of(logging.getLogger(name)) for name in TOUCHED_LOGGER_NAMES]
    logger_class = logging.getLoggerClass()
    structlog_was_configured = structlog.is_configured()
    structlog_configuration = structlog.get_config()
    process_hook = sys.excepthook
    thread_hook = threading.excepthook
    warning_hook = warnings.showwarning
    unraisable_hook = sys.unraisablehook
    propagator = get_global_textmap()
    carrying_trace = is_enabled()
    coding_asyncio = asyncio_records.is_enabled()
    try:
        configure(service)
        yield
    finally:
        sys.excepthook = process_hook
        threading.excepthook = thread_hook
        warnings.showwarning = warning_hook
        sys.unraisablehook = unraisable_hook
        set_global_textmap(propagator)
        set_enabled(carrying_trace)
        asyncio_records.set_enabled(coding_asyncio)
        logging.setLoggerClass(logger_class)
        for state in logger_states:
            state.restore()
        if structlog_was_configured:
            structlog.configure(**structlog_configuration)
        else:
            structlog.reset_defaults()
