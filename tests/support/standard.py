import json
import re
from importlib.resources import files

_RULES = json.loads(files("logline").joinpath("standard.json").read_text(encoding="utf-8"))

denied_field_names: tuple[str, ...] = tuple(sorted(_RULES["denied_field_names"]))
denied_field_suffixes: tuple[str, ...] = tuple(_RULES["denied_field_suffixes"])
event_name_pattern: re.Pattern[str] = re.compile(_RULES["event_name_pattern"])
