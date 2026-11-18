"""The filter on `uvicorn.error` that drops records which carry content and names every other."""

import errno
import logging
from dataclasses import dataclass

from logline.websocket import in_owned_session

__all__: list[str] = []

_UVICORN_WEBSOCKET_LINE = '"WebSocket %s"'
_UVICORN_ASGI_EXCEPTION_TEMPLATE = "Exception in ASGI application\n"
# websockets' template for a refused upgrade, which both of its servers write on the logger
# uvicorn hands them.
_WEBSOCKETS_REFUSAL_TEMPLATE = "connection rejected (%d %s)"


def _carries_content(record: logging.LogRecord) -> bool:
    """uvicorn's WebSocket lines carry the raw path with its query; websockets, which gets the
    `uvicorn.error` logger as its protocol logger, writes the request path, every header and every
    frame at DEBUG.
    """
    return record.levelno < logging.INFO or _UVICORN_WEBSOCKET_LINE in str(record.msg)


@dataclass(frozen=True)
class _CodedEvent:
    """`number_field` names the unit-named field that carries the template's first argument."""

    event: str
    reason: str | None = None
    number_field: str | None = None


_ASGI_APP_BROKEN = "asgi_app.contract_broken"
_HTTP_REFUSED = "http_request.refused"
_SERVER_SHUTDOWN_WAITING = "server_shutdown.waiting"
# The record of a template logline does not name, whatever uvicorn, websockets or a host writes.
_UNKNOWN_EVENT = _CodedEvent("uvicorn.unknown_record")
# uvicorn's and websockets' message templates; every protocol implementation shares them. A
# template that carries a path, a host, a port or an app's value has its arguments left out.
_CODED_EVENTS_BY_TEMPLATE = {
    "Invalid HTTP request received.": _CodedEvent(_HTTP_REFUSED, "invalid_request"),
    "Invalid HTTP/2 frame received: %s": _CodedEvent(_HTTP_REFUSED, "invalid_request"),
    "Unsupported upgrade request.": _CodedEvent(_HTTP_REFUSED, "unsupported_upgrade"),
    "No supported WebSocket library detected. Please use \"pip install 'uvicorn[standard]'\", "
    "or install 'websockets' or 'wsproto' manually.": _CodedEvent(
        _HTTP_REFUSED, "websocket_library_missing"
    ),
    "Exceeded concurrency limit.": _CodedEvent(_HTTP_REFUSED, "concurrency_limit"),
    "ASGI callable returned without starting response.": _CodedEvent(
        _ASGI_APP_BROKEN, "response_not_started"
    ),
    "ASGI callable returned without completing response.": _CodedEvent(
        _ASGI_APP_BROKEN, "response_not_completed"
    ),
    "ASGI callable should return None, but returned '%s'.": _CodedEvent(
        _ASGI_APP_BROKEN, "value_returned"
    ),
    "Invalid UTF-8 sequence received from client.": _CodedEvent(
        "websocket_frame.refused", "invalid_utf8"
    ),
    "ASGI 'lifespan' protocol appears unsupported.": _CodedEvent("app_lifespan.unsupported"),
    "Started server process [%d]": _CodedEvent("server_process.started"),
    "Maximum request limit of %d exceeded. Terminating process.": _CodedEvent(
        "server_process.limit_reached"
    ),
    "Shutting down": _CodedEvent("server_shutdown.started"),
    "Waiting for connections to close. (CTRL+C to force quit)": _CodedEvent(
        _SERVER_SHUTDOWN_WAITING, "connections"
    ),
    "Waiting for background tasks to complete. (CTRL+C to force quit)": _CodedEvent(
        _SERVER_SHUTDOWN_WAITING, "background_tasks"
    ),
    "Cancel %s running task(s), timeout graceful shutdown exceeded": _CodedEvent(
        "server_shutdown.timed_out", number_field="task_count"
    ),
    "Finished server process [%d]": _CodedEvent("server_process.finished"),
    _WEBSOCKETS_REFUSAL_TEMPLATE: _CodedEvent("websocket_upgrade.refused", number_field="status"),
}
# Each says what another line says: "Waiting for" what the app's lifespan lines report, the
# lifespan's own outcome lines and failure record, which the middleware writes from the ASGI
# messages with their traceback, websockets' "connection open", and uvicorn's two records for an
# app that returned without answering an upgrade, which the middleware's session line carries.
# websockets' "connection closed" repeats the end that the session line or the refusal line
# reports, or, when the handshake ended in a dropped client or a timeout, belongs to a connection
# that never became a WebSocket, which has no session line and no refusal line.
_DROPPED_TEMPLATES = frozenset(
    {
        "Waiting for application startup.",
        "Waiting for application shutdown.",
        "Application startup complete.",
        "Application shutdown complete.",
        "Application startup failed. Exiting.",
        "Application shutdown failed. Exiting.",
        "Exception in 'lifespan' protocol\n",
        "connection open",
        "connection closed",
        "ASGI callable returned without completing handshake.",
        "ASGI callable returned without sending handshake.",
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


def _first_integer_argument(record: logging.LogRecord) -> int | None:
    arguments = record.args
    if isinstance(arguments, tuple) and arguments and isinstance(arguments[0], int):
        return arguments[0]
    return None


def _coded_event_of_template(template: object) -> _CodedEvent:
    if not isinstance(template, str):
        return _UNKNOWN_EVENT
    if template.startswith(_LISTENING_TEMPLATE_PREFIX):
        return _LISTENING_EVENT
    return _CODED_EVENTS_BY_TEMPLATE.get(template, _UNKNOWN_EVENT)


def _name_record(record: logging.LogRecord) -> None:
    """Rewrite a uvicorn or websockets record into a literal event, with a code or a number.

    A record is known by its message template or by the `OSError` uvicorn logs for a failed bind,
    never by its formatted text; any other record becomes `uvicorn.unknown_record`. The rewritten
    record keeps its level and carries no message text, host, port, pid or path. Only the ASGI
    exception keeps its traceback.
    """
    fields: dict[str, str | int | None] = {}
    if isinstance(record.msg, OSError):
        coded = _bind_failure(record.msg)
        fields["error_type"] = type(record.msg).__name__
    elif record.msg == _UVICORN_ASGI_EXCEPTION_TEMPLATE:
        coded = _CodedEvent("asgi_app.failed")
        fields["error_type"] = _logged_exception_type_name(record)
    else:
        coded = _coded_event_of_template(record.msg)
        record.exc_info = None
        if coded.number_field:
            fields[coded.number_field] = _first_integer_argument(record)
    if coded.reason:
        fields["reason"] = coded.reason
    record.msg = coded.event
    record.args = ()
    record.__dict__.update({key: value for key, value in fields.items() if value is not None})


def _is_redundant(record: logging.LogRecord) -> bool:
    """A record whose message another line already carries.

    A refusal the app's session line carries is written, in the app's task, while the middleware
    owns it; a refusal outside any session, such as a malformed upgrade, has no session line.
    """
    if record.msg == _WEBSOCKETS_REFUSAL_TEMPLATE:
        return in_owned_session()
    return isinstance(record.msg, str) and record.msg in _DROPPED_TEMPLATES


class _UvicornErrorRecords(logging.Filter):
    """Drop the `uvicorn.error` records that carry content or repeat another, and name the rest.

    The logger passes INFO records whatever the root level, so that a refusal is seen when the root
    drops it; the filter drops a record the root level would drop.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not logging.getLogger().isEnabledFor(record.levelno):
            return False
        if _carries_content(record) or _is_redundant(record):
            return False
        _name_record(record)
        return True


# One instance: Logger.addFilter ignores an instance it already holds, so a second `configure`
# adds nothing.
UVICORN_ERROR_RECORDS = _UvicornErrorRecords()
