"""The filter on `uvicorn.error` that drops records which carry content and names uvicorn's own."""

import errno
import logging
from contextvars import ContextVar
from dataclasses import dataclass

__all__: list[str] = []

_UVICORN_WEBSOCKET_LINE = '"WebSocket %s"'
_UVICORN_ASGI_EXCEPTION_TEMPLATE = "Exception in ASGI application\n"
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
    "Started server process [%d]": _CodedEvent("server_process.started"),
    "Shutting down": _CodedEvent("server_shutdown.started"),
    "Finished server process [%d]": _CodedEvent("server_process.finished"),
}
# Each says what another line says: "Waiting for" what the app's lifespan lines report, and the
# lifespan's own outcome lines and failure record, which the middleware writes from the ASGI
# messages with their traceback.
_DROPPED_TEMPLATES = frozenset(
    {
        "Waiting for application startup.",
        "Waiting for application shutdown.",
        "Application startup complete.",
        "Application shutdown complete.",
        "Application startup failed. Exiting.",
        "Application shutdown failed. Exiting.",
        "Exception in 'lifespan' protocol\n",
    }
)
# uvicorn names the address it listens on in several forms (an IPv4 or IPv6 URL, a unix socket, a
# file descriptor's socket); each template begins alike and carries host, port or path.
_LISTENING_TEMPLATE_PREFIX = "Uvicorn running on "
_LISTENING_EVENT = _CodedEvent("server_listen.started")


def _bind_failure(error: OSError) -> _CodedEvent:
    reason = "port_taken" if error.errno == errno.EADDRINUSE else "os_error"
    return _CodedEvent("server_bind.failed", reason)


def _logged_exception_type_name(record: logging.LogRecord) -> str | None:
    # uvicorn logs `exc_info=exc`, which the logging module expands to the full triple.
    exc_info = record.exc_info
    if isinstance(exc_info, tuple) and exc_info and isinstance(exc_info[0], type):
        return exc_info[0].__name__
    return None


def _refusal_status(record: logging.LogRecord) -> int | None:
    arguments = record.args
    if isinstance(arguments, tuple) and arguments and isinstance(arguments[0], int):
        return arguments[0]
    return None


def _coded_event_of_template(template: object) -> _CodedEvent | None:
    if not isinstance(template, str):
        return None
    if template.startswith(_LISTENING_TEMPLATE_PREFIX):
        return _LISTENING_EVENT
    return _CODED_EVENTS_BY_TEMPLATE.get(template)


def _name_known_record(record: logging.LogRecord) -> None:
    """Rewrite a uvicorn or websockets record into a literal event, with a code or a status.

    A record is known by its message template or by the `OSError` uvicorn logs for a failed bind,
    never by its formatted text. The rewritten record keeps its level and carries no message text,
    host, port, pid or path; the ASGI exception keeps its traceback. Any other record stays
    unchanged.
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
        fields["status"] = _refusal_status(record)
    elif not (coded := _coded_event_of_template(record.msg)):
        return
    if coded.reason:
        fields["reason"] = coded.reason
    record.msg = coded.event
    record.args = ()
    record.__dict__.update({key: value for key, value in fields.items() if value is not None})


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
        if _carries_content(record) or _is_redundant(record):
            return False
        _name_known_record(record)
        return True


# One instance: Logger.addFilter ignores an instance it already holds, so a second `configure`
# adds nothing.
UVICORN_ERROR_RECORDS = _UvicornErrorRecords()
