import json

from logline.request import REQUEST_LOGGER_NAME

type Line = dict[str, object]

BASE_KEYS = {"ts", "level", "service", "version", "logger", "event", "trace_id"}


def json_lines(output: str) -> list[Line]:
    """Every non-empty line of `output` parsed as one JSON object; a line that is not one fails."""
    lines: list[Line] = []
    for text in output.splitlines():
        parsed = json.loads(text)
        assert isinstance(parsed, dict), text
        lines.append(parsed)
    return lines


def request_lines(lines: list[Line]) -> list[Line]:
    return [line for line in lines if line["logger"] == REQUEST_LOGGER_NAME]


def lines_of(lines: list[Line], logger: str) -> list[Line]:
    return [line for line in lines if line["logger"] == logger]
