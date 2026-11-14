"""The fleet's shared logger: one JSON record shape, a request line and W3C trace context."""

from logline.configuration import configure
from logline.request import (
    REQUEST_EVENT,
    RequestLineMiddleware,
    set_client_ip,
    set_route,
    set_sub,
)
from logline.trace import outgoing_headers

__version__ = "0.2.0"

__all__ = [
    "REQUEST_EVENT",
    "RequestLineMiddleware",
    "__version__",
    "configure",
    "outgoing_headers",
    "set_client_ip",
    "set_route",
    "set_sub",
]
