"""`configure`: the one call that makes every log line of a process a JSON object on stdout."""

import errno
import inspect
import logging
import os
import sys
import weakref
from dataclasses import dataclass
from types import TracebackType
from typing import cast

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
_UVICORN_WEBSOCKET_LINE = '"WebSocket %s"'
_UVICORN_ASGI_EXCEPTION_TEMPLATE = "Exception in ASGI application\n"
_UVICORN_LIFESPAN_MODULE = "uvicorn.lifespan.on"
_UVICORN_LIFESPAN_SEND = "send"


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


def _carries_content(record: logging.LogRecord) -> bool:
    """uvicorn's WebSocket lines carry the raw path with its query; websockets, which gets the
    `uvicorn.error` logger as its protocol logger, writes the request path, every header and every
    frame at DEBUG.
    """
    return record.levelno < logging.INFO or _UVICORN_WEBSOCKET_LINE in str(record.msg)


@dataclass(frozen=True)
class _CodedEvent:
    event: str
    reason: str | None = None


# uvicorn's message templates, which carry no argument; every protocol implementation shares them.
_CODED_EVENTS_BY_TEMPLATE = {
    "Invalid HTTP request received.": _CodedEvent("http_request.refused", "invalid_request"),
    "Unsupported upgrade request.": _CodedEvent("http_request.refused", "unsupported_upgrade"),
    "Application startup failed. Exiting.": _CodedEvent("app_startup.failed"),
    "Application shutdown failed. Exiting.": _CodedEvent("app_shutdown.failed"),
}


def _bind_failure(error: OSError) -> _CodedEvent:
    reason = "port_taken" if error.errno == errno.EADDRINUSE else "os_error"
    return _CodedEvent("server_bind.failed", reason)


_ExceptionInfo = tuple[type[BaseException], BaseException, TracebackType]


def _logged_exception_type_name(record: logging.LogRecord) -> str:
    # uvicorn logs `exc_info=exc`, which the logging module expands to the full triple.
    exception_type, _, _ = cast("_ExceptionInfo", record.exc_info)
    return exception_type.__name__


@dataclass(frozen=True)
class _LifespanCall:
    function: str
    protocol: object


def _lifespan_call() -> _LifespanCall | None:
    """The uvicorn lifespan call that is logging the current record, if one is."""
    frame = inspect.currentframe()
    while frame and frame.f_globals.get("__name__") != _UVICORN_LIFESPAN_MODULE:
        frame = frame.f_back
    return _LifespanCall(frame.f_code.co_name, frame.f_locals["self"]) if frame else None


# uvicorn logs the failure message an app sends with its lifespan failure, which is free text and,
# under Starlette, the traceback with the exception message, and then logs its own coded template.
# The message is dropped; the exception its sender was handling moves onto the template's line.
# Both records come from one lifespan protocol object, which keys the exception.
_LIFESPAN_FAILURES: weakref.WeakKeyDictionary[object, BaseException | None] = (
    weakref.WeakKeyDictionary()
)


def _take_lifespan_failure(call: _LifespanCall | None) -> BaseException | None:
    return _LIFESPAN_FAILURES.pop(call.protocol, None) if call else None


def _name_known_record(record: logging.LogRecord, call: _LifespanCall | None) -> None:
    """Rewrite uvicorn's own warning and error record into a literal event and a code.

    A record is known by its message template or by the `OSError` uvicorn logs for a failed bind,
    never by its formatted text. The rewritten record keeps its level and carries no message text,
    host, port or path; the ASGI exception and a lifespan failure keep their traceback. Any other
    record stays unchanged.
    """
    fields: dict[str, str | None] = {}
    if isinstance(record.msg, OSError):
        coded = _bind_failure(record.msg)
        fields["error_type"] = type(record.msg).__name__
    elif record.msg == _UVICORN_ASGI_EXCEPTION_TEMPLATE:
        coded = _CodedEvent("asgi_app.failed")
        fields["error_type"] = _logged_exception_type_name(record)
    elif isinstance(record.msg, str) and record.msg in _CODED_EVENTS_BY_TEMPLATE:
        coded = _CODED_EVENTS_BY_TEMPLATE[record.msg]
        if failure := _take_lifespan_failure(call):
            record.exc_info = (type(failure), failure, failure.__traceback__)
            fields["error_type"] = type(failure).__name__
    else:
        return
    if coded.reason:
        fields["reason"] = coded.reason
    record.msg = coded.event
    record.args = ()
    record.__dict__.update(fields)


class _UvicornErrorRecords(logging.Filter):
    """Drop the `uvicorn.error` records that carry content and name the failure records."""

    def filter(self, record: logging.LogRecord) -> bool:
        call = _lifespan_call()
        if call and call.function == _UVICORN_LIFESPAN_SEND:
            _LIFESPAN_FAILURES[call.protocol] = sys.exception()
            return False
        if _carries_content(record):
            return False
        _name_known_record(record, call)
        return True


# One instance each: Logger.addFilter ignores an instance it already holds, so a second
# `configure` adds nothing.
_DROP_ALL = _DropAll()
_UVICORN_ERROR_RECORDS = _UvicornErrorRecords()


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
    logging.getLogger("uvicorn.error").addFilter(_UVICORN_ERROR_RECORDS)


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
    for name, level in _CAPPED_LOGGER_LEVELS.items():
        logging.getLogger(name).setLevel(level)
    _route_uvicorn_through_root()
