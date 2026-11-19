"""The fleet's shared logger: one JSON record shape, a request line and W3C trace context."""

from logline.configuration import configure
from logline.request import (
    REQUEST_EVENT,
    RequestLine,
    RequestLineMiddleware,
    set_client_ip,
    set_route,
    set_sub,
)
from logline.step import Outcome, StepTimer
from logline.trace import current_traceparent, outgoing_headers, unit_trace

__version__ = "1.4.0"

__all__ = [
    "REQUEST_EVENT",
    "Outcome",
    "RequestLine",
    "RequestLineMiddleware",
    "StepTimer",
    "__version__",
    "configure",
    "current_traceparent",
    "outgoing_headers",
    "set_client_ip",
    "set_route",
    "set_sub",
    "unit_trace",
]
