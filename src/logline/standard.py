"""The machine rules of the logging standard, read from the package's `standard.json`."""

import json
import re
from importlib.resources import files

__all__ = [
    "BASE_KEYS",
    "DENIED_FIELD_NAMES",
    "DENIED_FIELD_SUFFIXES",
    "EVENT_NAME_PATTERN",
    "is_denied_field",
]

_RULES = json.loads(files("logline").joinpath("standard.json").read_text(encoding="utf-8"))

BASE_KEYS: frozenset[str] = frozenset(_RULES["base_keys"])
DENIED_FIELD_NAMES: frozenset[str] = frozenset(_RULES["denied_field_names"])
DENIED_FIELD_SUFFIXES: tuple[str, ...] = tuple(_RULES["denied_field_suffixes"])
EVENT_NAME_PATTERN: re.Pattern[str] = re.compile(_RULES["event_name_pattern"])


def is_denied_field(name: str) -> bool:
    """Whether a line may not carry a field of this name."""
    return name in DENIED_FIELD_NAMES or name.endswith(DENIED_FIELD_SUFFIXES)
