"""W3C trace context. OpenTelemetry's context is the one owner of the running request's trace."""

import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry import context, trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

__all__ = ["current_trace_id", "outgoing_headers", "request_trace"]

# Used directly, never through opentelemetry.propagate: no baggage and no OTEL_PROPAGATORS switch.
_PROPAGATOR = TraceContextTextMapPropagator()
_TRACE_ID_BITS = 128
_SPAN_ID_BITS = 64


def _non_zero_random(bits: int) -> int:
    return secrets.randbelow(2**bits - 1) + 1


@contextmanager
def request_trace(incoming_headers: Mapping[str, str]) -> Iterator[None]:
    """Run the block inside the request's trace: the incoming one, else a new one.

    `incoming_headers` maps lower-case header names to values; only `traceparent` and `tracestate`
    are read. The block gets a fresh span id either way.
    """
    parent_context = _PROPAGATOR.extract(incoming_headers, context=context.Context())
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
    token = context.attach(trace.set_span_in_context(trace.NonRecordingSpan(span_context)))
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
