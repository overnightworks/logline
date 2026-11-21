"""A propagator with a span header other than `traceparent`, as a host's OTEL_PROPAGATORS names."""

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.propagators.textmap import (
    CarrierT,
    Getter,
    Setter,
    TextMapPropagator,
    default_getter,
    default_setter,
)

SPAN_HEADER = "x-host-span"


class SingleHeaderPropagator(TextMapPropagator):
    """Carries the span as `<trace id hex>-<span id hex>` in one header, `SPAN_HEADER`."""

    @property
    def fields(self) -> set[str]:
        return {SPAN_HEADER}

    def extract(
        self, carrier: CarrierT, context: Context | None = None, getter: Getter = default_getter
    ) -> Context:
        values = getter.get(carrier, SPAN_HEADER)
        if not values:
            return trace.set_span_in_context(trace.INVALID_SPAN, context)
        trace_id, span_id = values[0].split("-")
        span_context = trace.SpanContext(
            trace_id=int(trace_id, 16), span_id=int(span_id, 16), is_remote=True
        )
        return trace.set_span_in_context(trace.NonRecordingSpan(span_context), context)

    def inject(
        self, carrier: CarrierT, context: Context | None = None, setter: Setter = default_setter
    ) -> None:
        span_context = trace.get_current_span(context).get_span_context()
        if span_context.is_valid:
            value = f"{trace.format_trace_id(span_context.trace_id)}-"
            value += trace.format_span_id(span_context.span_id)
            setter.set(carrier, SPAN_HEADER, value)
