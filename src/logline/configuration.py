"""`configure`: the one call that makes every log line of a process a JSON object on stdout."""

import logging
import os
import sys

import structlog
from structlog.processors import JSONRenderer
from structlog.stdlib import BoundLogger, LoggerFactory, ProcessorFormatter, filter_by_level

from logline.process_failures import install_process_failure_hooks
from logline.record import record_shape
from logline.request import REQUEST_LOGGER_NAME
from logline.uvicorn_records import UVICORN_ERROR_RECORDS

__all__ = ["configure"]

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}
_DEFAULT_LEVEL_NAME = "info"
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
# Each carries content below its cap: a URL with its query, an MCP session id, a postgres
# server notice, and spoken text and phonemes, which piper writes at WARNING.
_CAPPED_LOGGER_LEVELS = {
    "httpx": logging.WARNING,
    "httpx2": logging.WARNING,
    "httpcore": logging.WARNING,
    "httpcore2": logging.WARNING,
    "mcp": logging.WARNING,
    "sqlalchemy.dialects.postgresql": logging.WARNING,
    "piper": logging.ERROR,
}
_ROOT_LOGGER_NAME = ""


# The loggers `configure` changes, for a caller that restores them.
TOUCHED_LOGGER_NAMES = (
    _ROOT_LOGGER_NAME,
    REQUEST_LOGGER_NAME,
    *_UVICORN_LOGGERS,
    *_CAPPED_LOGGER_LEVELS,
)


class _DropAll(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return False


# One instance: Logger.addFilter ignores an instance it already holds, so a second `configure`
# adds nothing.
_DROP_ALL = _DropAll()


def _root_level() -> int:
    name = os.environ.get("LOG_LEVEL", _DEFAULT_LEVEL_NAME)
    try:
        return _LEVELS[name.lower()]
    except KeyError:
        accepted = ", ".join(_LEVELS)
        raise ValueError(f"LOG_LEVEL is {name!r}; accepted names are {accepted}") from None


def _json_stdout_handler(processors: list[structlog.typing.Processor]) -> logging.Handler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        ProcessorFormatter(
            foreign_pre_chain=processors,
            processors=[ProcessorFormatter.remove_processors_meta, JSONRenderer()],
        )
    )
    return handler


def _route_uvicorn_through_root(root_level: int) -> None:
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.setLevel(logging.NOTSET)
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").addFilter(_DROP_ALL)
    uvicorn_error = logging.getLogger("uvicorn.error")
    uvicorn_error.setLevel(min(root_level, logging.INFO))
    uvicorn_error.addFilter(UVICORN_ERROR_RECORDS)


def configure(service: str) -> None:
    """Make every log line of this process one JSON object on stdout; call once at the entry point.

    `service` names the app or service. `SERVICE_VERSION` is read here and `LOG_LEVEL` sets the
    root level; a malformed `LOG_LEVEL` raises `ValueError` before anything changes. It also writes
    an uncaught exception, a thread's uncaught exception and a Python warning as record lines.
    """
    root_level = _root_level()
    processors = record_shape(service, os.environ.get("SERVICE_VERSION") or None)
    structlog.configure(
        processors=[filter_by_level, *processors, ProcessorFormatter.wrap_for_formatter],
        logger_factory=LoggerFactory(),
        wrapper_class=BoundLogger,
        cache_logger_on_first_use=False,
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(_json_stdout_handler(processors))
    root.setLevel(root_level)
    logging.getLogger(REQUEST_LOGGER_NAME).setLevel(logging.INFO)
    for name, level in _CAPPED_LOGGER_LEVELS.items():
        logging.getLogger(name).setLevel(max(level, root_level))
    _route_uvicorn_through_root(root_level)
    install_process_failure_hooks()
