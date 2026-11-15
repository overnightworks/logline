"""`configure`: the one call that makes every log line of a process a JSON object on stdout."""

import logging
import os
import sys

import structlog
from structlog.processors import JSONRenderer
from structlog.stdlib import BoundLogger, LoggerFactory, ProcessorFormatter, filter_by_level

from logline.record import record_shape
from logline.request import REQUEST_LOGGER_NAME

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
# Their INFO lines carry the full URL with its query.
_CLIENT_LOGGERS_WITH_URLS = ("httpx", "httpx2", "httpcore", "httpcore2")
_ROOT_LOGGER_NAME = ""
_UVICORN_WEBSOCKET_LINE = '"WebSocket %s"'


# The loggers `configure` changes, for a caller that restores them.
TOUCHED_LOGGER_NAMES = (
    _ROOT_LOGGER_NAME,
    REQUEST_LOGGER_NAME,
    *_UVICORN_LOGGERS,
    *_CLIENT_LOGGERS_WITH_URLS,
)


class _DropAll(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return False


class _DropUvicornContentRecords(logging.Filter):
    """uvicorn's WebSocket lines carry the raw path with its query; websockets, which gets this
    logger as its protocol logger, writes the request path, every header and every frame at DEBUG.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.INFO and _UVICORN_WEBSOCKET_LINE not in str(record.msg)


# One instance each: Logger.addFilter ignores an instance it already holds, so a second
# `configure` adds nothing.
_DROP_ALL = _DropAll()
_DROP_UVICORN_CONTENT_RECORDS = _DropUvicornContentRecords()


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


def _route_uvicorn_through_root() -> None:
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.setLevel(logging.NOTSET)
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").addFilter(_DROP_ALL)
    logging.getLogger("uvicorn.error").addFilter(_DROP_UVICORN_CONTENT_RECORDS)


def configure(service: str) -> None:
    """Make every log line of this process one JSON object on stdout; call once at the entry point.

    `service` names the app or service. `SERVICE_VERSION` is read here and `LOG_LEVEL` sets the
    root level; a malformed `LOG_LEVEL` raises `ValueError` before anything changes.
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
    for name in _CLIENT_LOGGERS_WITH_URLS:
        logging.getLogger(name).setLevel(logging.WARNING)
    _route_uvicorn_through_root()
