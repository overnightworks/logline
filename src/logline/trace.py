"""W3C trace context. OpenTelemetry's context is the one owner of the running request's trace."""

import secrets
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import cast

from opentelemetry import context, propagate, trace

from logline.standard import PARENT_SPAN_ID_KEY, SPAN_ID_KEY

__all__ = [
    "current_trace_id",
    "current_traceparent",
    "outgoing_headers",
    "request_trace",
    "unit_trace",
]

_TRACE_ID_BITS = 128
_SPAN_ID_BITS = 64
# The span that started the current one; the span itself is OpenTelemetry's.
_PARENT_SPAN_ID = context.create_key("logline.parent_span_id")


def _non_zero_random(bits: int) -> int:
    return secrets.randbelow(2**bits - 1) + 1


type HeaderPairs = Iterable[tuple[str, str]] | Iterable[tuple[bytes, bytes]]


def _text(header_part: str | bytes) -> str:
    return header_part.decode("latin-1") if isinstance(header_part, bytes) else header_part


def _trace_headers(headers: HeaderPairs) -> dict[str, str]:
    fields = propagate.get_global_textmap().fields
    carrier: dict[str, str] = {}
    for name, value in headers:
        field = _text(name).lower()
        if field in fields:
            carrier[field] = _text(value)
    return carrier


def _incoming_span(carrier: dict[str, str]) -> trace.SpanContext:
    """The span `carrier` names, invalid when it names none."""
    return trace.get_current_span(
        propagate.extract(carrier, context=context.Context())
    ).get_span_context()


def _with_span(span_context: trace.SpanContext, parent_span_id: str | None) -> context.Context:
    """The current context with `span_context` as its span, started by `parent_span_id`."""
    with_span = trace.set_span_in_context(trace.NonRecordingSpan(span_context))
    return context.set_value(_PARENT_SPAN_ID, parent_span_id, with_span)


def _child_context(parent: trace.SpanContext) -> context.Context:
    """The current context with a fresh span of `parent`'s trace, else of a new one."""
    if parent.is_valid:
        trace_id, trace_flags, trace_state = parent.trace_id, parent.trace_flags, parent.trace_state
        parent_span_id = trace.format_span_id(parent.span_id)
    else:
        trace_id = _non_zero_random(_TRACE_ID_BITS)
        trace_flags = trace.TraceFlags(trace.TraceFlags.DEFAULT)
        trace_state = None
        parent_span_id = None
    span_context = trace.SpanContext(
        trace_id=trace_id,
        span_id=_non_zero_random(_SPAN_ID_BITS),
        is_remote=False,
        trace_flags=trace_flags,
        trace_state=trace_state,
    )
    return _with_span(span_context, parent_span_id)


@contextmanager
def request_trace(raw_headers: HeaderPairs) -> Iterator[context.Context]:
    """Run the block inside the request's trace: the incoming one, else a new one.

    `raw_headers` are (name, value) pairs of text, as a standard-library server hands them over, or
    the ASGI scope's byte pairs; names are read case-insensitively and only the fields of
    OpenTelemetry's global propagator are used (`traceparent` and `tracestate` under `configure`'s
    default). The block gets a fresh span id either way, and its parent is the incoming span, if
    any. The block's context is what the block yields, for `span_fields`.

    An exception that leaves the block leaves the trace attached: the server writes its error line
    after the block, in the same task, and that line belongs to this trace.
    """
    span = _child_context(_incoming_span(_trace_headers(raw_headers)))
    token = context.attach(span)
    yield span  # NOSONAR(S9152) an exception skips the detach on purpose, see the docstring
    context.detach(token)


@contextmanager
def unit_trace(traceparent: str | None) -> Iterator[None]:
    """Run the block inside a unit's trace, such as a WebSocket turn: the incoming one or a new one.

    `traceparent` is the incoming message's value, or None. A valid one is continued as it is: the
    unit has no span of its own, so what the block hands on, outside a `StepTimer` block, names the
    sender's span. A malformed one, or any under a global propagator that does not read
    `traceparent`, starts a new trace with a fresh span, as it does for a request.

    The trace is detached when the block ends, an exception included, which propagates unchanged: a
    later line of a long-lived task must not carry this unit's trace.
    """
    incoming = _incoming_span({} if traceparent is None else {"traceparent": traceparent})
    span = _with_span(incoming, None) if incoming.is_valid else _child_context(incoming)
    token = context.attach(span)
    try:
        yield
    finally:
        context.detach(token)


def open_step_span() -> context.Context | None:
    """A fresh span under the current one, as a context not yet attached; None outside a trace."""
    current = trace.get_current_span().get_span_context()
    return _child_context(current) if current.is_valid else None


def enter_span(span: context.Context) -> context.Context:
    """Make `span` current and return the context it replaced."""
    previous = context.get_current()
    context.attach(span)
    return previous


def leave_span(span: context.Context, previous: context.Context) -> None:
    """Put `previous` back if `span` is still current, else leave the current context alone.

    This is never `context.detach(token)`: a token from another context makes OpenTelemetry log
    "Failed to detach context" with a traceback, and a block that ends in another task, such as a
    dropped generator's finaliser, has no business changing that task's context.
    """
    if context.get_current() is span:
        context.attach(previous)


@contextmanager
def attached(span: context.Context | None) -> Iterator[None]:
    """Run the block, such as one log call, with `span` current, attached and detached here.

    With None the block runs in the context it finds.
    """
    token = None if span is None else context.attach(span)
    try:
        yield
    finally:
        if token is not None:
            context.detach(token)


def span_fields(span: context.Context | None) -> dict[str, str | None]:
    """The two ids of a closing line: the span `span` holds and its parent, as 16 hex digits.

    Both are None for no span, as for a step outside a trace; the parent alone is None for the first
    span of a trace.
    """
    if span is None:
        return {SPAN_ID_KEY: None, PARENT_SPAN_ID_KEY: None}
    span_id = trace.format_span_id(trace.get_current_span(span).get_span_context().span_id)
    parent_span_id = cast("str | None", context.get_value(_PARENT_SPAN_ID, span))
    return {SPAN_ID_KEY: span_id, PARENT_SPAN_ID_KEY: parent_span_id}


def current_trace_id() -> str | None:
    """The running trace's id as 32 hex digits: the request's, the session's or the turn's.

    None outside a trace.
    """
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return trace.format_trace_id(span_context.trace_id)


def outgoing_headers() -> dict[str, str]:
    """The current context as headers for an outgoing HTTP call, as the global propagator gives it.

    Inside a `StepTimer` block the step's span, else inside a turn the sender's, else the request's
    or the WebSocket session's. Outside a trace there is no span to hand on: the result is empty
    under `configure`'s default, and holds only what the global propagator carries beyond a span,
    such as baggage, under one that carries more.
    """
    headers: dict[str, str] = {}
    propagate.inject(headers)
    return headers


def current_traceparent() -> str | None:
    """The current trace as a `traceparent` value for a message.

    Inside a `StepTimer` block the step's span, else inside a turn the sender's, outside one the
    WebSocket session's; None outside a trace.
    """
    return outgoing_headers().get("traceparent")
