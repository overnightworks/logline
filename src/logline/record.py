"""The record shape: the processors that give every line the same base fields."""

from logging import Logger

from structlog.processors import TimeStamper, format_exc_info
from structlog.stdlib import ExtraAdder, add_log_level, add_logger_name
from structlog.typing import EventDict, Processor, WrappedLogger

from logline.trace import current_trace_id

__all__ = ["record_shape"]


_EXTRA_FIELDS = ExtraAdder()
_SCALAR_TYPES = (str, int, float, bool, type(None))
# uvicorn's coloured copy of the message.
_UNCOPIED_EXTRA_FIELDS = frozenset({"event", "color_message"})


def _copy_extra_fields(logger: Logger, method_name: str, event_dict: EventDict) -> EventDict:
    """Copy the scalar `extra=` fields of a standard-library record; a structlog event has none."""
    record = event_dict.get("_record")
    if record is None:
        return event_dict
    extra_fields = _EXTRA_FIELDS(logger, method_name, {"_record": record})
    for key, value in extra_fields.items():
        if (
            key != "_record"
            and key not in _UNCOPIED_EXTRA_FIELDS
            and isinstance(value, _SCALAR_TYPES)
        ):
            event_dict[key] = value
    return event_dict


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

    A standard-library record's scalar `extra=` fields are copied first, so each stamp overwrites a
    caller's key of the same name; `event` and uvicorn's `color_message` are not copied. An
    exception renders as Python's text traceback in `exception`: frames, messages and the whole
    chain, never local variables.
    """
    return [
        _copy_extra_fields,
        add_logger_name,
        add_log_level,
        TimeStamper(fmt="iso", utc=True, key="ts"),
        _identity_processor(service, version),
        _add_trace_id,
        format_exc_info,
    ]
