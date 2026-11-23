"""Check one Python source against the machine rules of the fleet's logging standard.

`check` is pure: it parses the text and never imports or runs the checked code. A logger it cannot
trace to a binding in the same file (one handed in without an annotation, one bound in another
file) is not judged, and neither is a field given through `**fields` or an `extra=` variable.
"""

import ast
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum, StrEnum
from pathlib import PurePath

from logline.standard import (
    CLOSING_EVENT_SUFFIX,
    EVENT_NAME_PATTERN,
    OWN_SETUP_ASSIGNMENTS,
    OWN_SETUP_CALLS,
    is_denied_field,
)

__all__ = ["Finding", "Rule", "check"]


class Rule(StrEnum):
    """The rows of the check; each value is the code a host sees."""

    EVENT_NOT_LITERAL = "event_not_literal"
    EVENT_NAME_SHAPE = "event_name_shape"
    MESSAGE_ARGS = "message_args"
    DENIED_FIELD = "denied_field"
    OWN_SETUP = "own_setup"
    UVICORN_LOG_CONFIG = "uvicorn_log_config"
    UVICORN_LOG_LEVEL = "uvicorn_log_level"
    UVICORN_OPTIONS_HIDDEN = "uvicorn_options_hidden"
    HAND_WRITTEN_ENDED = "hand_written_ended"


@dataclass(frozen=True)
class Finding:
    """One broken rule at a place: `line` and `column` count from 1."""

    path: PurePath
    line: int
    column: int
    rule: Rule


class _Kind(Enum):
    STDLIB = "stdlib"
    STRUCTLOG = "structlog"


_LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical", "log"})
_STDLIB_FACTORIES = frozenset({"logging.getLogger"})
_STRUCTLOG_FACTORIES = frozenset({"structlog.get_logger", "structlog.stdlib.get_logger"})
_ANNOTATION_KINDS = {
    "logging.Logger": _Kind.STDLIB,
    "structlog.BoundLogger": _Kind.STRUCTLOG,
    "structlog.stdlib.BoundLogger": _Kind.STRUCTLOG,
}
_STEP_TIMERS = frozenset({"logline.StepTimer", "logline.step.StepTimer"})
_FIELD_BINDERS = frozenset({"structlog.contextvars.bind_contextvars"})
_UVICORN_STARTS = frozenset({"uvicorn.run", "uvicorn.main.run", "uvicorn.Config"})

_Row = tuple[ast.expr | ast.keyword | ast.stmt, Rule]


def check(source: str, path: PurePath) -> tuple[Finding, ...]:
    """The rows the source breaks, in source order. `path` only labels them.

    Raises `SyntaxError` for a source that does not parse.
    """
    tree = ast.parse(source, filename=str(path))
    judge = _Judge(tree)
    lines = re.split(r"\r\n|\r|\n", source)
    findings = {
        Finding(path, node.lineno, _character_column(lines[node.lineno - 1], node), rule)
        for node, rule in judge.rows()
    }
    return tuple(sorted(findings, key=lambda finding: (finding.line, finding.column, finding.rule)))


def _character_column(line: str, node: ast.expr | ast.keyword | ast.stmt) -> int:
    # `col_offset` counts UTF-8 bytes, the record's column counts characters.
    return len(line.encode()[: node.col_offset].decode()) + 1


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _is_literal_text(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_hand_written_ended(node: ast.expr | None) -> bool:
    return (
        _is_matching_literal(node)
        and isinstance(node, ast.Constant)
        and str(node.value).endswith(CLOSING_EVENT_SUFFIX)
    )


def _is_matching_literal(node: ast.expr | None) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and EVENT_NAME_PATTERN.fullmatch(node.value) is not None
    )


class _CallsOfName:
    """The calls of one name, indexed once so each parameter's verdict is a lookup."""

    def __init__(self) -> None:
        self.total = 0
        self._with_keyword: defaultdict[str, list[ast.keyword]] = defaultdict(list)
        self._matching_at: dict[bool, Counter[int]] = {False: Counter(), True: Counter()}
        self._calls_with_keyword: defaultdict[str, list[ast.Call]] = defaultdict(list)
        self._ended_keywords: set[str] = set()
        self._ended_at: dict[bool, Counter[int]] = {False: Counter(), True: Counter()}

    def add(self, call: ast.Call, *, bound: bool) -> None:
        self.total += 1
        for keyword in call.keywords:
            if keyword.arg is not None:
                self._with_keyword[keyword.arg].append(keyword)
                self._calls_with_keyword[keyword.arg].append(call)
                if _is_hand_written_ended(keyword.value):
                    self._ended_keywords.add(keyword.arg)
        for index, argument in enumerate(call.args):
            if isinstance(argument, ast.Starred):
                break
            if _is_matching_literal(argument):
                self._matching_at[bound][index] += 1
            if _is_hand_written_ended(argument):
                self._ended_at[bound][index] += 1

    def all_pass_literal(self, parameter: str, position: int | None, *, is_method: bool) -> bool:
        """Whether every call passes `parameter` a matching literal, by keyword or by position."""
        if not all(_is_matching_literal(k.value) for k in self._with_keyword[parameter]):
            return False
        if position is None:
            return len(self._calls_with_keyword[parameter]) == self.total
        matching = self._matching_count(position, is_method=is_method)
        for call in self._calls_with_keyword[parameter]:
            matching -= self._matches_by_position(call, position, is_method=is_method)
        return matching == self.total - len(self._calls_with_keyword[parameter])

    def any_pass_hand_written_ended(
        self, parameter: str, position: int | None, *, is_method: bool
    ) -> bool:
        """Whether some call passes `parameter` a literal event that ends in the closing suffix."""
        if parameter in self._ended_keywords:
            return True
        if position is None:
            return False
        return any(
            counter[position - 1 if is_method and bound else position] > 0
            for bound, counter in self._ended_at.items()
            if position - (1 if is_method and bound else 0) >= 0
        )

    def _matching_count(self, position: int, *, is_method: bool) -> int:
        return sum(
            counter[position - 1 if is_method and bound else position]
            for bound, counter in self._matching_at.items()
            if position - (1 if is_method and bound else 0) >= 0
        )

    @staticmethod
    def _matches_by_position(call: ast.Call, position: int, *, is_method: bool) -> int:
        index = position - (1 if is_method and isinstance(call.func, ast.Attribute) else 0)
        leading = call.args[: index + 1]
        if index < 0 or len(leading) <= index or any(isinstance(a, ast.Starred) for a in leading):
            return 0
        return int(_is_matching_literal(leading[index]))


class _Judge:
    """The facts one file states about its names, and the rows its calls and assignments break."""

    def __init__(self, tree: ast.Module) -> None:
        self._tree = tree
        self._parents: dict[ast.AST, ast.AST] = {}
        self._imports: dict[str, str] = {}
        self._loggers: dict[str, _Kind] = {}
        self._timers: set[str] = set()
        self._calls_by_name: defaultdict[str, _CallsOfName] = defaultdict(_CallsOfName)
        self._parameter_verdicts: dict[tuple[bool, str, str, int | None, bool], bool] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                self._parents[child] = parent
        for node in ast.walk(tree):
            self._read_import(node)
        for node in ast.walk(tree):
            self._read_binding(node)
            self._read_call_name(node)

    def rows(self) -> Iterator[_Row]:
        for node in ast.walk(self._tree):
            if isinstance(node, ast.Call):
                yield from self._call_rows(node)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if self._resolve(target) in OWN_SETUP_ASSIGNMENTS:
                        yield target, Rule.OWN_SETUP

    def _read_import(self, node: ast.AST) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    self._imports[alias.asname] = alias.name
                else:
                    root = alias.name.split(".")[0]
                    self._imports[root] = root
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                self._imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def _read_call_name(self, node: ast.AST) -> None:
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                self._calls_by_name[node.func.id].add(node, bound=False)
            elif isinstance(node.func, ast.Attribute):
                self._calls_by_name[node.func.attr].add(node, bound=True)

    def _read_binding(self, node: ast.AST) -> None:
        if isinstance(node, ast.Assign):
            kind = self._factory_kind(node.value)
            for target in node.targets:
                self._bind(target, kind, self._is_step_timer(node.value))
        elif isinstance(node, ast.AnnAssign):
            kind = self._annotation_kind(node.annotation)
            if kind is None and node.value is not None:
                kind = self._factory_kind(node.value)
            timer = node.value is not None and self._is_step_timer(node.value)
            self._bind(node.target, kind, timer)
        elif isinstance(node, ast.arg) and node.annotation is not None:
            self._bind_key(node.arg, self._annotation_kind(node.annotation), False)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            kind = self._factory_kind(node.context_expr)
            self._bind(node.optional_vars, kind, self._is_step_timer(node.context_expr))

    def _bind(self, target: ast.expr, kind: _Kind | None, is_timer: bool) -> None:
        key = self._key(target)
        if key is not None:
            self._bind_key(key, kind, is_timer)

    def _bind_key(self, key: str, kind: _Kind | None, is_timer: bool) -> None:
        if kind is not None:
            self._loggers[key] = kind
        if is_timer:
            self._timers.add(key)

    @staticmethod
    def _key(node: ast.expr) -> str | None:
        dotted = _dotted(node)
        if dotted is None:
            return None
        return dotted.removeprefix("self.").removeprefix("cls.")

    def _resolve(self, node: ast.expr) -> str | None:
        dotted = _dotted(node)
        if dotted is None:
            return None
        head, _, rest = dotted.partition(".")
        resolved = self._imports.get(head, head)
        return f"{resolved}.{rest}" if rest else resolved

    def _annotation_kind(self, annotation: ast.expr) -> _Kind | None:
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            return self._annotation_kind(annotation.left) or self._annotation_kind(annotation.right)
        resolved = self._resolve(annotation)
        return None if resolved is None else _ANNOTATION_KINDS.get(resolved)

    def _factory_kind(self, node: ast.expr) -> _Kind | None:
        if not isinstance(node, ast.Call):
            return None
        if isinstance(node.func, ast.Attribute) and node.func.attr == "bind":
            return _Kind.STRUCTLOG
        resolved = self._resolve(node.func)
        if resolved in _STDLIB_FACTORIES:
            return _Kind.STDLIB
        if resolved in _STRUCTLOG_FACTORIES:
            return _Kind.STRUCTLOG
        return None

    def _logger_kind(self, receiver: ast.expr) -> _Kind | None:
        if isinstance(receiver, ast.Call):
            return self._factory_kind(receiver)
        key = self._key(receiver)
        if key in self._loggers:
            return self._loggers[key]
        return _Kind.STDLIB if self._resolve(receiver) == "logging" else None

    def _is_step_timer(self, node: ast.expr) -> bool:
        return isinstance(node, ast.Call) and self._resolve(node.func) in _STEP_TIMERS

    def _is_timer(self, receiver: ast.expr) -> bool:
        return self._is_step_timer(receiver) or self._key(receiver) in self._timers

    def _call_rows(self, call: ast.Call) -> Iterator[_Row]:
        resolved = self._resolve(call.func)
        if resolved in OWN_SETUP_CALLS:
            yield call, Rule.OWN_SETUP
        if resolved in _UVICORN_STARTS:
            yield from _uvicorn_rows(call)
        if resolved in _STEP_TIMERS:
            if call.args:
                yield from self._event_rows(call.args[0])
            yield from _field_rows(call.keywords)
        if resolved in _FIELD_BINDERS:
            yield from _field_rows(call.keywords)
        if not isinstance(call.func, ast.Attribute):
            return
        method, receiver = call.func.attr, call.func.value
        if method == "bind" or (method == "add" and self._is_timer(receiver)):
            yield from _field_rows(call.keywords)
        elif method in _LOG_METHODS:
            kind = self._logger_kind(receiver)
            if kind is not None:
                yield from self._logging_rows(call, method, kind)

    def _logging_rows(self, call: ast.Call, method: str, kind: _Kind) -> Iterator[_Row]:
        event_index = 1 if method == "log" else 0
        arguments = call.args
        if any(isinstance(argument, ast.Starred) for argument in arguments[: event_index + 1]):
            yield arguments[0], Rule.EVENT_NOT_LITERAL
        elif len(arguments) > event_index:
            yield from self._event_rows(arguments[event_index])
            if self._is_hand_written_ended_event(arguments[event_index]):
                yield arguments[event_index], Rule.HAND_WRITTEN_ENDED
        if kind is _Kind.STDLIB and len(arguments) > event_index + 1:
            yield arguments[event_index + 1], Rule.MESSAGE_ARGS
        for keyword in call.keywords:
            if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
                yield from _extra_rows(keyword.value)
            elif kind is _Kind.STRUCTLOG:
                yield from _field_rows([keyword])

    def _event_rows(self, event: ast.expr) -> Iterator[_Row]:
        if _is_literal_text(event):
            if not _is_matching_literal(event):
                yield event, Rule.EVENT_NAME_SHAPE
        elif not self._is_literal_parameter(event):
            yield event, Rule.EVENT_NOT_LITERAL

    def _is_hand_written_ended_event(self, event: ast.expr) -> bool:
        """Whether `event` is a literal, or a parameter some call in this file passes a literal,
        that ends in the closing suffix."""
        if _is_literal_text(event):
            return _is_hand_written_ended(event)
        return self._parameter_verdict(event, ended=True)

    def _event_parameter(
        self, event: ast.expr
    ) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, int | None, bool] | None:
        """The function, the position and whether it is a method, when `event` is its parameter."""
        if not isinstance(event, ast.Name):
            return None
        function = self._enclosing_function(event)
        if function is None:
            return None
        parameters = function.args
        positional = [argument.arg for argument in (*parameters.posonlyargs, *parameters.args)]
        keyword_only = [argument.arg for argument in parameters.kwonlyargs]
        if event.id not in (*positional, *keyword_only):
            return None
        is_method = isinstance(self._parents.get(function), ast.ClassDef)
        position = positional.index(event.id) if event.id in positional else None
        return function, position, is_method

    def _is_literal_parameter(self, event: ast.expr) -> bool:
        """Whether `event` is a parameter that every call in this file passes a matching literal."""
        return self._parameter_verdict(event, ended=False)

    def _parameter_verdict(self, event: ast.expr, *, ended: bool) -> bool:
        """What the calls in this file say about the event parameter: that some call passes a
        hand-written `.ended` literal (`ended`), or that every call passes a matching literal."""
        parameter = self._event_parameter(event)
        if parameter is None or not isinstance(event, ast.Name):
            return False
        function, position, is_method = parameter
        key = (ended, function.name, event.id, position, is_method)
        if key not in self._parameter_verdicts:
            calls = self._calls_by_name.get(function.name)
            self._parameter_verdicts[key] = calls is not None and (
                calls.any_pass_hand_written_ended(event.id, position, is_method=is_method)
                if ended
                else calls.all_pass_literal(event.id, position, is_method=is_method)
            )
        return self._parameter_verdicts[key]

    def _enclosing_function(self, node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        parent = self._parents.get(node)
        while parent is not None:
            if isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef):
                return parent
            parent = self._parents.get(parent)
        return None


def _field_rows(keywords: list[ast.keyword]) -> Iterator[_Row]:
    for keyword in keywords:
        if keyword.arg is not None and is_denied_field(keyword.arg):
            yield keyword, Rule.DENIED_FIELD


def _extra_rows(extra: ast.Dict) -> Iterator[_Row]:
    for key in extra.keys:
        if (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and is_denied_field(key.value)
        ):
            yield key, Rule.DENIED_FIELD


def _uvicorn_rows(call: ast.Call) -> Iterator[_Row]:
    options_hidden = (
        len(call.args) > 1
        or any(isinstance(a, ast.Starred) for a in call.args)
        or any(keyword.arg is None for keyword in call.keywords)
    )
    if options_hidden:
        yield call, Rule.UVICORN_OPTIONS_HIDDEN
    named = {keyword.arg: keyword for keyword in call.keywords if keyword.arg is not None}
    log_config = named.get("log_config")
    if log_config is None:
        if not options_hidden:
            yield call, Rule.UVICORN_LOG_CONFIG
    elif not (isinstance(log_config.value, ast.Constant) and log_config.value.value is None):
        yield log_config, Rule.UVICORN_LOG_CONFIG
    if "log_level" in named:
        yield named["log_level"], Rule.UVICORN_LOG_LEVEL
