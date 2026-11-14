"""The record shape: the processors that give every line the same base fields."""

from structlog.processors import TimeStamper, format_exc_info
from structlog.stdlib import add_log_level, add_logger_name
from structlog.typing import EventDict, Processor, WrappedLogger

from logline.trace import current_trace_id

__all__ = ["record_shape"]


def _add_trace_id(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    event_dict["trace_id"] = current_trace_id()
    return event_dict


def _identity_processor(service: str, version: str | None) -> Processor:
    def add_identity(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
        event_dict["service"] = service
        event_dict["version"] = version
        return event_dict

    return add_identity


def record_shape(service: str, version: str | None) -> list[Processor]:
    """The processors that stamp `ts`, `level`, `service`, `version`, `logger` and `trace_id`.

    Each overwrites a caller's key of the same name. An exception renders as Python's text
    traceback in `exception`: frames, messages and the whole chain, never local variables.
    """
    return [
        add_logger_name,
        add_log_level,
        TimeStamper(fmt="iso", utc=True, key="ts"),
        _identity_processor(service, version),
        _add_trace_id,
        format_exc_info,
    ]
