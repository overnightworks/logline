"""The filter on `uvicorn.error` that drops records which carry content and names uvicorn's own."""

import errno
import inspect
import logging
import sys
import weakref
from contextvars import ContextVar
from dataclasses import dataclass
from types import TracebackType
from typing import cast

__all__: list[str] = []

_UVICORN_WEBSOCKET_LINE = '"WebSocket %s"'
_UVICORN_ASGI_EXCEPTION_TEMPLATE = "Exception in ASGI application\n"
_UVICORN_LIFESPAN_MODULE = "uvicorn.lifespan.on"
_UVICORN_LIFESPAN_SEND = "send"
# websockets' template for a refused upgrade, which both of its servers write on the logger
# uvicorn hands them, and uvicorn's for an app that returned without completing the handshake.
_WEBSOCKETS_REFUSAL_TEMPLATE = "connection rejected (%d %s)"
_UVICORN_INCOMPLETE_HANDSHAKE_TEMPLATE = "ASGI callable returned without completing handshake."
# uvicorn's websockets-sansio protocol writes the refusal and then the incomplete-handshake line
# from one task, which it creates for the connection; the task's context carries what the first
# record saw to the second.
_UPGRADE_REFUSED_IN_TASK: ContextVar[bool] = ContextVar("upgrade_refused_in_task", default=False)


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


# uvicorn's message templates, which carry no argument or only a process id; every protocol
# implementation shares them.
_CODED_EVENTS_BY_TEMPLATE = {
    "Invalid HTTP request received.": _CodedEvent("http_request.refused", "invalid_request"),
    "Unsupported upgrade request.": _CodedEvent("http_request.refused", "unsupported_upgrade"),
    _UVICORN_INCOMPLETE_HANDSHAKE_TEMPLATE: _CodedEvent(
        "websocket_handshake.failed", "handshake_incomplete"
    ),
    "Application startup failed. Exiting.": _CodedEvent("app_startup.failed"),
    "Application shutdown failed. Exiting.": _CodedEvent("app_shutdown.failed"),
    "Started server process [%d]": _CodedEvent("server_process.started"),
    "Application startup complete.": _CodedEvent("app_startup.completed"),
    "Shutting down": _CodedEvent("server_shutdown.started"),
    "Application shutdown complete.": _CodedEvent("app_shutdown.completed"),
    "Finished server process [%d]": _CodedEvent("server_process.finished"),
}
# Each says what the line after it says.
_DROPPED_TEMPLATES = frozenset(
    {"Waiting for application startup.", "Waiting for application shutdown."}
)
# uvicorn names the address it listens on in several forms (an IPv4 or IPv6 URL, a unix socket, a
# file descriptor's socket); each template begins alike and carries host, port or path.
_LISTENING_TEMPLATE_PREFIX = "Uvicorn running on "
_LISTENING_EVENT = _CodedEvent("server_listen.started")


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


def _coded_event_of_template(template: object) -> _CodedEvent | None:
    if not isinstance(template, str):
        return None
    if template.startswith(_LISTENING_TEMPLATE_PREFIX):
        return _LISTENING_EVENT
    return _CODED_EVENTS_BY_TEMPLATE.get(template)


def _name_known_record(record: logging.LogRecord, call: _LifespanCall | None) -> None:
    """Rewrite a uvicorn or websockets record into a literal event, with a code or a status.

    A record is known by its message template or by the `OSError` uvicorn logs for a failed bind,
    never by its formatted text. The rewritten record keeps its level and carries no message text,
    host, port, pid or path; the ASGI exception and a lifespan failure keep their traceback. Any
    other record stays unchanged.
    """
    fields: dict[str, str | int | None] = {}
    if isinstance(record.msg, OSError):
        coded = _bind_failure(record.msg)
        fields["error_type"] = type(record.msg).__name__
    elif record.msg == _UVICORN_ASGI_EXCEPTION_TEMPLATE:
        coded = _CodedEvent("asgi_app.failed")
        fields["error_type"] = _logged_exception_type_name(record)
    elif record.msg == _WEBSOCKETS_REFUSAL_TEMPLATE:
        coded = _CodedEvent("websocket_upgrade.refused")
        fields["status"] = cast("tuple[int, str]", record.args)[0]
    elif coded := _coded_event_of_template(record.msg):
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


def _is_redundant(record: logging.LogRecord) -> bool:
    """A record whose message another record of uvicorn or websockets already carries."""
    if record.msg == _UVICORN_INCOMPLETE_HANDSHAKE_TEMPLATE:
        return _UPGRADE_REFUSED_IN_TASK.get()
    return isinstance(record.msg, str) and record.msg in _DROPPED_TEMPLATES


class _UvicornErrorRecords(logging.Filter):
    """Drop the `uvicorn.error` records that carry content or repeat another, and name the rest.

    The logger passes INFO records whatever the root level, so that a refusal is seen when the root
    drops it; the filter drops a record the root level would drop.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg == _WEBSOCKETS_REFUSAL_TEMPLATE:
            _UPGRADE_REFUSED_IN_TASK.set(True)
        if not logging.getLogger().isEnabledFor(record.levelno):
            return False
        try:
            return self._keeps(record)
        except Exception:  # noqa: BLE001 - see below
            # The mapping reads uvicorn's frames, locals and templates, and logline declares no
            # uvicorn bound; a filter that raises propagates into uvicorn's own code, which
            # would turn a changed uvicorn into a crash. The record passes as written unless it
            # carries content, which is dropped whenever the filter cannot tell it is safe.
            return self._passes_without_the_mapping(record)

    @staticmethod
    def _passes_without_the_mapping(record: logging.LogRecord) -> bool:
        return not _carries_content(record)

    def _keeps(self, record: logging.LogRecord) -> bool:
        call = _lifespan_call()
        if call and call.function == _UVICORN_LIFESPAN_SEND:
            _LIFESPAN_FAILURES[call.protocol] = sys.exception()
            return False
        if _carries_content(record) or _is_redundant(record):
            return False
        _name_known_record(record, call)
        return True


# One instance: Logger.addFilter ignores an instance it already holds, so a second `configure`
# adds nothing.
UVICORN_ERROR_RECORDS = _UvicornErrorRecords()
