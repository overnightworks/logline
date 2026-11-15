"""W3C trace context. OpenTelemetry's context is the one owner of the running request's trace."""

import secrets
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import Token

from opentelemetry import context, trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

__all__ = [
    "current_trace_id",
    "current_traceparent",
    "outgoing_headers",
    "request_trace",
    "unit_trace",
]

# Used directly, never through opentelemetry.propagate: no baggage and no OTEL_PROPAGATORS switch.
_PROPAGATOR = TraceContextTextMapPropagator()
_TRACE_ID_BITS = 128
_SPAN_ID_BITS = 64


def _non_zero_random(bits: int) -> int:
    return secrets.randbelow(2**bits - 1) + 1


def _trace_headers(raw_headers: Iterable[tuple[bytes, bytes]]) -> dict[str, str]:
    fields = _PROPAGATOR.fields
    decoded = ((name.decode("latin-1"), value.decode("latin-1")) for name, value in raw_headers)
    return {name: value for name, value in decoded if name in fields}


def _attach_span(carrier: dict[str, str]) -> Token[context.Context]:
    """Attach a fresh span of the trace `carrier` continues, else of a new one; return the token."""
    parent_context = _PROPAGATOR.extract(carrier, context=context.Context())
    parent = trace.get_current_span(parent_context).get_span_context()
    if parent.is_valid:
        trace_id, trace_flags, trace_state = parent.trace_id, parent.trace_flags, parent.trace_state
    else:
        trace_id = _non_zero_random(_TRACE_ID_BITS)
        trace_flags = trace.TraceFlags(trace.TraceFlags.DEFAULT)
        trace_state = None
    span_context = trace.SpanContext(
        trace_id=trace_id,
        span_id=_non_zero_random(_SPAN_ID_BITS),
        is_remote=False,
        trace_flags=trace_flags,
        trace_state=trace_state,
    )
    return context.attach(trace.set_span_in_context(trace.NonRecordingSpan(span_context)))


@contextmanager
def request_trace(raw_headers: Iterable[tuple[bytes, bytes]]) -> Iterator[None]:
    """Run the block inside the request's trace: the incoming one, else a new one.

    `raw_headers` are the ASGI scope's headers, lower-case names as bytes; only `traceparent` and
    `tracestate` are read. The block gets a fresh span id either way.

    An exception that leaves the block leaves the trace attached: the server writes its error line
    after the block, in the same task, and that line belongs to this trace.
    """
    token = _attach_span(_trace_headers(raw_headers))
    yield  # NOSONAR(S9152) an exception skips the detach on purpose, see the docstring
    context.detach(token)


@contextmanager
def unit_trace(traceparent: str | None) -> Iterator[None]:
    """Run the block inside a unit's trace, such as a WebSocket turn: the incoming one or a new one.

    `traceparent` is the incoming message's value, or None. A malformed one starts a new trace, as
    it does for a request. The block gets a fresh span id either way.

    The trace is detached when the block ends, an exception included, which propagates unchanged: a
    later line of a long-lived task must not carry this unit's trace.
    """
    token = _attach_span({} if traceparent is None else {"traceparent": traceparent})
    try:
        yield
    finally:
        context.detach(token)


def current_trace_id() -> str | None:
    """The running request's trace id as 32 hex digits, or None outside a request."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return trace.format_trace_id(span_context.trace_id)


def outgoing_headers() -> dict[str, str]:
    """The current trace context as headers for an outgoing HTTP call; empty outside a request."""
    headers: dict[str, str] = {}
    _PROPAGATOR.inject(headers)
    return headers


def current_traceparent() -> str | None:
    """The current trace as a `traceparent` value for a message, or None outside a trace."""
    return outgoing_headers().get("traceparent")
