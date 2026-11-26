"""`configure`: the one call that makes every log line of a process a JSON object on stdout."""

import logging
import os
import sys

import structlog
from opentelemetry.environment_variables import OTEL_PROPAGATORS
from opentelemetry.propagate import set_global_textmap
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from structlog.processors import JSONRenderer
from structlog.stdlib import BoundLogger, LoggerFactory, ProcessorFormatter, filter_by_level

from logline.asyncio_records import ASYNCIO_LOGGER_NAME, install_asyncio_records
from logline.outgoing_trace import install_trace_carriers
from logline.process_failures import install_process_failure_hooks
from logline.record import record_shape
from logline.request import REQUEST_LOGGER_NAME
from logline.uvicorn_records import UVICORN_ERROR_RECORDS

__all__: list[str] = []

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
# server notice, the credentials file path botocore writes at INFO, and spoken text and
# phonemes, which piper writes at WARNING.
_CAPPED_LOGGER_LEVELS = {
    "httpx": logging.WARNING,
    "httpx2": logging.WARNING,
    "httpcore": logging.WARNING,
    "httpcore2": logging.WARNING,
    "mcp": logging.WARNING,
    "botocore": logging.WARNING,
    "sqlalchemy.dialects.postgresql": logging.WARNING,
    "piper": logging.ERROR,
}
_ROOT_LOGGER_NAME = ""


# The loggers `configure` changes, for a caller that restores them.
TOUCHED_LOGGER_NAMES = (
    _ROOT_LOGGER_NAME,
    REQUEST_LOGGER_NAME,
    *_UVICORN_LOGGERS,
    ASYNCIO_LOGGER_NAME,
    *_CAPPED_LOGGER_LEVELS,
)


class _DropAll(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return False


# One instance: Logger.addFilter ignores an instance it already holds, so a second `configure`
# adds nothing.
_DROP_ALL = _DropAll()


def _root_level() -> int:
    name = os.environ.get("LOG_LEVEL", _DEFAULT_LEVEL_NAME)
    try:
        return _LEVELS[name.lower()]
    except KeyError:
        accepted = ", ".join(_LEVELS)
        raise ValueError(f"LOG_LEVEL is {name!r}; accepted names are {accepted}") from None


def _set_global_propagator() -> None:
    # OpenTelemetry's own loader reads the variable the same way: empty counts as unset.
    if not os.environ.get(OTEL_PROPAGATORS):
        set_global_textmap(TraceContextTextMapPropagator())


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
    uvicorn_error = logging.getLogger("uvicorn.error")
    # INFO whatever the root level: a refusal is INFO and must be seen under a quiet root, and
    # websockets' DEBUG frames, which carry content, never reach the filter under a debug root.
    uvicorn_error.setLevel(logging.INFO)
    uvicorn_error.addFilter(UVICORN_ERROR_RECORDS)


def configure(service: str) -> None:
    """Make every log line of this process one JSON object on stdout; call once at the entry point.

    `service` names the app or service. `SERVICE_VERSION` is read here and `LOG_LEVEL` sets the
    root level; a malformed `LOG_LEVEL` raises `ValueError` before anything changes. It also writes
    an uncaught exception, a thread's uncaught exception, a Python warning, an unraisable exception
    and every failure asyncio reports (an exception nobody retrieved, in a callback or on a
    transport) as record lines; a host's own `loop.set_exception_handler` still wins over the last.

    While `OTEL_PROPAGATORS` is unset or empty it also sets OpenTelemetry's global propagator to
    W3C trace context alone, so no baggage is continued or forwarded by any library of the process;
    a set variable leaves the global as OpenTelemetry built it from it.

    It also hands the global propagator's headers, `traceparent` and `tracestate` under that
    default, to every outgoing HTTP request of the process that goes through `http.client`
    (`urllib.request`, urllib3, requests, botocore) or through httpx or httpx2 where installed, with
    no call-site code. A request that already carries `traceparent` keeps it and gets no
    propagator header, and `OTEL_PROPAGATORS=none` adds none. `outgoing_headers()` stays for
    transports these do not cover, such as `websockets` and aiohttp.

    Every work item submitted to a `concurrent.futures.ThreadPoolExecutor` also runs with the
    OpenTelemetry context its submitter had, so `Executor.map`, asyncio's `run_in_executor` and
    boto3's managed transfers carry the trace; a thread a host or library starts and the other
    executors do not.
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
        logging.getLogger(name).setLevel(max(level, root_level))
    _route_uvicorn_through_root()
    _set_global_propagator()
    install_trace_carriers()
    install_asyncio_records()
    install_process_failure_hooks()
