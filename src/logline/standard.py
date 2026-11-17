"""The machine rules of the logging standard, read from the package's `standard.json`."""

import json
from importlib.resources import files

__all__ = [
    "BASE_KEYS",
    "is_denied_field",
]

_RULES = json.loads(files("logline").joinpath("standard.json").read_text(encoding="utf-8"))

BASE_KEYS: frozenset[str] = frozenset(_RULES["base_keys"])
_DENIED_FIELD_NAMES: frozenset[str] = frozenset(_RULES["denied_field_names"])
_DENIED_FIELD_SUFFIXES: tuple[str, ...] = tuple(_RULES["denied_field_suffixes"])


def is_denied_field(name: str) -> bool:
    """Whether a line may not carry a field of this name."""
    return name in _DENIED_FIELD_NAMES or name.endswith(_DENIED_FIELD_SUFFIXES)
