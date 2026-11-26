"""Names every failure asyncio reports as a coded error line, with the trace of what failed.

asyncio writes its failures as free text on the logger `asyncio`: a message with the object's repr
and, in debug mode, a source traceback. Two parts replace that text, both installed once per
process by `install_asyncio_records`:

- a wrap of `asyncio.BaseEventLoop.default_exception_handler`, never removed, behind an on or off
  state that decides whether it writes anything; a host's own `loop.set_exception_handler` still
  wins, and a host handler that falls back to the default handler gets the coded line. It reads
  the failure from the handler's context dict, so the line is coded by what failed; the message
  only tells an unretrieved or shielded future and a failed client_connected_cb apart. It writes
  the line inside the failed task's or callback's own context, so the line carries that object's
  trace;
- a filter on the logger `asyncio`, which names every other record from INFO up: one a loop that
  is not asyncio's own writes, or asyncio writes outside its handler.

Neither raises into asyncio.
"""

import asyncio
import contextvars
import functools
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from logline.record import logged_exception_type_name

__all__: list[str] = []

ASYNCIO_LOGGER_NAME = "asyncio"
_ASYNCIO_LOGGER = logging.getLogger(ASYNCIO_LOGGER_NAME)
_TASK_FAILED = "asyncio_task.failed"
_CALLBACK_FAILED = "asyncio_callback.failed"
_UNKNOWN_RECORD = "asyncio.unknown_record"
_CODED_EVENTS = frozenset({_TASK_FAILED, _CALLBACK_FAILED, _UNKNOWN_RECORD})
# The message asyncio's streams give a failed `client_connected_cb`, whose context names no task.
_CLIENT_CONNECTED_FAILED = "Unhandled exception in client_connected_cb"
# The endings of the messages asyncio gives a future whose exception nobody retrieved, and one a
# cancelled `shield` could not hand on.
_UNRETRIEVED_FUTURE_ENDINGS = (" exception was never retrieved", " exception in shielded future")
# The keys whose object carries the context of the failed work, in the order asyncio itself picks
# the context a host's exception handler runs in.
_OWNER_KEYS = ("task", "future", "handle")


@dataclass
class _Coding:
    enabled: bool = False
    installed: bool = False


_coding = _Coding()


def _exception_of(context: Mapping[str, object]) -> BaseException | None:
    exception = context.get("exception")
    return exception if isinstance(exception, BaseException) else None


def _is_task_failure(context: Mapping[str, object]) -> bool:
    message = context.get("message")
    return (
        context.get("task") is not None
        or (
            context.get("future") is not None
            and isinstance(message, str)
            and message.endswith(_UNRETRIEVED_FUTURE_ENDINGS)
        )
        or message == _CLIENT_CONNECTED_FAILED
    )


def _event_of(context: Mapping[str, object], exception: BaseException | None) -> str:
    if exception is None:
        return _UNKNOWN_RECORD
    return _TASK_FAILED if _is_task_failure(context) else _CALLBACK_FAILED


def _owner_context(context: Mapping[str, object]) -> contextvars.Context:
    """The context of the failed task or callback; empty where asyncio hands logline none.

    A plain future and a failure that names no task or handle have none, and the line then carries
    no trace, never the one that happens to be current.
    """
    owner = next((thing for key in _OWNER_KEYS if (thing := context.get(key)) is not None), None)
    get_context = getattr(owner, "get_context", None)
    owned = get_context() if callable(get_context) else None
    return owned if isinstance(owned, contextvars.Context) else contextvars.Context()


def _is_entered(context: contextvars.Context) -> bool:
    try:
        context.run(lambda: None)
    except RuntimeError:
        return True
    return False


def _run_in(context: contextvars.Context, action: Callable[[], None]) -> None:
    # `Context.run` refuses a context that is entered, which is the current one when a host's
    # handler, which asyncio runs in the failed object's context, calls the default handler.
    if _is_entered(context):
        action()
    else:
        context.run(action)


def _log_coded(event: str, exception: BaseException | None) -> None:
    if exception is None:
        _ASYNCIO_LOGGER.error(event)
    else:
        _ASYNCIO_LOGGER.error(
            event, exc_info=exception, extra={"error_type": type(exception).__name__}
        )


def _wrote_coded_line(context: Mapping[str, object]) -> bool:
    exception = _exception_of(context)
    try:
        _run_in(
            _owner_context(context), lambda: _log_coded(_event_of(context, exception), exception)
        )
    # Nothing raises into asyncio: its own record, which the filter names, takes over.
    except Exception:  # noqa: BLE001
        return False
    return True


def _wrap_default_exception_handler() -> None:
    default_exception_handler = asyncio.BaseEventLoop.default_exception_handler

    @functools.wraps(default_exception_handler)
    def code_the_failure(self: asyncio.BaseEventLoop, context: Mapping[str, object]) -> None:
        if not (_coding.enabled and _wrote_coded_line(context)):
            default_exception_handler(self, context)  # pyright: ignore[reportArgumentType]

    asyncio.BaseEventLoop.default_exception_handler = code_the_failure


def _rewrite(record: logging.LogRecord, event: str, error_type: str | None) -> None:
    record.msg = event
    record.args = ()
    record.exc_text = None
    record.stack_info = None
    if error_type is None:
        record.exc_info = None
    else:
        record.levelno = logging.ERROR
        record.levelname = logging.getLevelName(logging.ERROR)
        record.error_type = error_type


class _AsyncioRecords(logging.Filter):
    """Name the record asyncio writes outside the wrapped handler, and pass the coded ones.

    A record below INFO passes as asyncio wrote it. One with an exception becomes
    `asyncio_callback.failed` at error and is written again in an empty context, since nothing
    says which trace it belongs to; one without becomes `asyncio.unknown_record` at its own level.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.INFO or (
            isinstance(record.msg, str) and record.msg in _CODED_EVENTS
        ):
            return True
        error_type = logged_exception_type_name(record)
        if error_type is None:
            _rewrite(record, _UNKNOWN_RECORD, None)
            return True
        _rewrite(record, _CALLBACK_FAILED, error_type)
        contextvars.Context().run(_ASYNCIO_LOGGER.handle, record)
        return False


# One instance: Logger.addFilter ignores an instance it already holds, so a second `configure`
# adds nothing.
ASYNCIO_RECORDS = _AsyncioRecords()


def install_asyncio_records() -> None:
    """Wrap asyncio's default exception handler once, turn the coding on and filter the logger."""
    if not _coding.installed:
        _wrap_default_exception_handler()
        _coding.installed = True
    _coding.enabled = True
    _ASYNCIO_LOGGER.addFilter(ASYNCIO_RECORDS)


def is_enabled() -> bool:
    return _coding.enabled


def set_enabled(enabled: bool) -> None:
    """Turn the coding on or off; the wrap is never removed."""
    _coding.enabled = enabled
