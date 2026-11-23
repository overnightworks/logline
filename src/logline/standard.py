"""The machine rules of the logging standard, read from the package's `standard.json`."""

import json
import re
from importlib.resources import files

__all__: list[str] = []

_RULES = json.loads(files("logline").joinpath("standard.json").read_text(encoding="utf-8"))

BASE_KEYS: frozenset[str] = frozenset(_RULES["base_keys"])
# The span a closing line is, then the span that started it, in the order `closing_line_keys` lists.
SPAN_ID_KEY: str
PARENT_SPAN_ID_KEY: str
SPAN_ID_KEY, PARENT_SPAN_ID_KEY = _RULES["closing_line_keys"]
CLOSING_LINE_KEYS: frozenset[str] = frozenset({SPAN_ID_KEY, PARENT_SPAN_ID_KEY})
CLOSING_EVENT_SUFFIX: str = _RULES["closing_event_suffix"]
EVENT_NAME_PATTERN: re.Pattern[str] = re.compile(_RULES["event_name_pattern"])
OWN_SETUP_CALLS: frozenset[str] = frozenset(_RULES["own_setup_calls"])
OWN_SETUP_ASSIGNMENTS: frozenset[str] = frozenset(_RULES["own_setup_assignments"])
_DENIED_FIELD_NAMES: frozenset[str] = frozenset(_RULES["denied_field_names"])
_DENIED_FIELD_SUFFIXES: tuple[str, ...] = tuple(_RULES["denied_field_suffixes"])


def is_denied_field(name: str) -> bool:
    """Whether a line may not carry a field of this name."""
    return name in _DENIED_FIELD_NAMES or name.endswith(_DENIED_FIELD_SUFFIXES)
