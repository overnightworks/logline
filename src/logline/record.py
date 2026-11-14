"""The record shape: the processors that give every line the same base fields."""

import traceback

from structlog.processors import ExceptionRenderer, TimeStamper
from structlog.stdlib import add_log_level, add_logger_name
from structlog.typing import EventDict, ExcInfo, Processor, WrappedLogger

from logline.trace import current_trace_id

__all__ = ["record_shape"]

_CAUSE = "The above exception was the direct cause of the following exception:"
_CONTEXT = "During handling of the above exception, another exception occurred:"


def _add_trace_id(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    event_dict["trace_id"] = current_trace_id()
    return event_dict


def _identity_processor(service: str, version: str | None) -> Processor:
    def add_identity(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
        event_dict["service"] = service
        event_dict["version"] = version
        return event_dict

    return add_identity


def _exception_without_messages(exc_info: ExcInfo) -> str:
    """The traceback of an exception chain with each exception's type and no message.

    A message is text the raiser composed, and a library's carries the URL it called, with its
    query and the `Location` header it was redirected to.
    """
    exception = exc_info[1]
    own = "".join(
        [
            "Traceback (most recent call last):\n",
            *traceback.format_tb(exception.__traceback__),
            f"{type(exception).__module__}.{type(exception).__qualname__}\n",
        ]
    )
    if exception.__cause__ is not None:
        return f"{_exception_without_messages(_info_of(exception.__cause__))}\n{_CAUSE}\n\n{own}"
    if exception.__context__ is not None and not exception.__suppress_context__:
        return (
            f"{_exception_without_messages(_info_of(exception.__context__))}\n{_CONTEXT}\n\n{own}"
        )
    return own


def _info_of(exception: BaseException) -> ExcInfo:
    return type(exception), exception, exception.__traceback__


def record_shape(service: str, version: str | None) -> list[Processor]:
    """The processors that stamp `ts`, `level`, `service`, `version`, `logger` and `trace_id`.

    Each overwrites a caller's key of the same name. Exceptions render as text, their frames and
    types without messages or local variables.
    """
    return [
        add_logger_name,
        add_log_level,
        TimeStamper(fmt="iso", utc=True, key="ts"),
        _identity_processor(service, version),
        _add_trace_id,
        ExceptionRenderer(_exception_without_messages),
    ]
