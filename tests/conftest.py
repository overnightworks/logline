import logging
import sys
import threading
import warnings
from collections.abc import Iterator
from dataclasses import replace

import pytest
import structlog
from opentelemetry.propagate import get_global_textmap, set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from structlog.stdlib import ProcessorFormatter

from logline.outgoing_trace import is_enabled, set_enabled
from logline.testing import LoggerState

pytest.register_assert_rewrite("tests.support")

from tests.support.loggers import all_loggers  # noqa: E402 - after the rewrite
from tests.support.outgoing_clients import EchoServer, echo_server  # noqa: E402 - after the rewrite


@pytest.fixture(autouse=True)
def isolated_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give each test the logging and propagator as they were, and no logline environment."""
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("SERVICE_VERSION", raising=False)
    monkeypatch.delenv("OTEL_PROPAGATORS", raising=False)
    propagator = get_global_textmap()
    carrying_trace = is_enabled()
    hooks = (sys.excepthook, threading.excepthook, warnings.showwarning)
    saved = {logger.name: LoggerState.of(logger) for logger in all_loggers()}
    yield
    set_global_textmap(propagator)
    set_enabled(carrying_trace)
    sys.excepthook, threading.excepthook, warnings.showwarning = hooks
    for logger in all_loggers():
        state = saved.get(logger.name) or LoggerState(logger, [], [], logging.NOTSET, True)
        if logger is logging.getLogger():
            # The root's handlers belong to pytest's capture, which swaps them between the phases of
            # a test, so restoring the snapshot would bring back the setup phase's; only the ones
            # `configure` added go.
            for handler in state.logger.handlers[:]:
                if isinstance(handler.formatter, ProcessorFormatter):
                    state.logger.removeHandler(handler)
            state = replace(state, handlers=state.logger.handlers[:])
        state.restore()
    structlog.reset_defaults()


@pytest.fixture
def host_with_otel_propagators_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the test the environment and propagator of a host that set `OTEL_PROPAGATORS=none`."""
    monkeypatch.setenv("OTEL_PROPAGATORS", "none")
    set_global_textmap(CompositePropagator([]))


@pytest.fixture
def echo() -> Iterator[EchoServer]:
    """A real `EchoServer`: a server that records the headers of every request it gets."""
    with echo_server() as server:
        yield server
