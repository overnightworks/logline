"""The record shape: the processors that give every line the same base fields."""

from logging import Logger
from math import isfinite

from structlog.processors import TimeStamper, format_exc_info
from structlog.stdlib import ExtraAdder, add_log_level, add_logger_name
from structlog.typing import EventDict, Processor, WrappedLogger

from logline.standard import BASE_KEYS, is_denied_field
from logline.trace import current_trace_id

__all__ = ["record_shape"]


_EXTRA_FIELDS = ExtraAdder()
_SCALAR_TYPES = (str, int, float, bool, type(None))
# `color_message` is uvicorn's coloured copy of the message; `exception` is the key
# `format_exc_info` writes, so a library's own field of that name must not pose as a traceback.
_UNCOPIED_EXTRA_FIELDS = BASE_KEYS | {"color_message", "exception"}
# `format_exc_info` reads `exc_info` from a structlog call; it carries the exception, not a field.
_EXCEPTION_REQUEST_KEYS = frozenset({"exc_info"})


def _is_line_value(value: object) -> bool:
    return isinstance(value, _SCALAR_TYPES)


def _json_safe(value: object) -> object:
    """A non-finite float is not JSON; it renders as `null`."""
    if isinstance(value, float) and not isfinite(value):
        return None
    return value


def _keep_scalar_fields(logger: Logger, method_name: str, event_dict: EventDict) -> EventDict:
    """Apply the one scalar rule: a field is text, a number, a boolean or `null`.

    A standard-library record's `extra=` fields are copied under it; a structlog event's own
    fields are filtered by it. Neither path lets a caller's `exception` pose as a traceback.
    """
    record = event_dict.get("_record")
    if record is None:
        kept = {
            key: _json_safe(value)
            for key, value in event_dict.items()
            if key in _EXCEPTION_REQUEST_KEYS or (key != "exception" and _is_line_value(value))
        }
        if "event" in event_dict:
            # A line keeps its `event`; a caller's non-text event renders as text.
            event = event_dict["event"]
            kept["event"] = event if isinstance(event, str) else str(event)
        return kept
    extra_fields = _EXTRA_FIELDS(logger, method_name, {"_record": record})
    for key, value in extra_fields.items():
        if key != "_record" and key not in _UNCOPIED_EXTRA_FIELDS and _is_line_value(value):
            event_dict[key] = _json_safe(value)
    return event_dict


def _drop_denied_fields(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Remove every field the standard's deny list names."""
    return {key: value for key, value in event_dict.items() if not is_denied_field(key)}


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

    A standard-library record's scalar `extra=` fields are copied first, and a structlog event keeps
    only its scalar fields, so each stamp overwrites a caller's key of the same name; `event`,
    `exception` and uvicorn's `color_message` are not copied. A non-finite float is written `null`.
    An exception renders as Python's text traceback in `exception`: frames, messages and the whole
    chain, never local variables. A field on the standard's deny list is dropped.
    """
    return [
        _keep_scalar_fields,
        add_logger_name,
        add_log_level,
        TimeStamper(fmt="iso", utc=True, key="ts"),
        _identity_processor(service, version),
        _add_trace_id,
        format_exc_info,
        _drop_denied_fields,
    ]
