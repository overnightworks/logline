"""The trace a caller sends, and the pattern a `traceparent` value is read by."""

import re

INCOMING_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
INCOMING_SPAN_ID = "00f067aa0ba902b7"
INCOMING_TRACEPARENT = f"00-{INCOMING_TRACE_ID}-{INCOMING_SPAN_ID}-01"
TRACEPARENT_PATTERN = re.compile(
    r"^00-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)
