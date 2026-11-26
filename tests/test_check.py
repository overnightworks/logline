import ast
import sys
from collections.abc import Callable
from pathlib import Path, PurePath

import pytest

import logline.check as check_module
from logline.check import Finding, Rule, check
from tests.support.check_cases import STDLIB, case, rules

_STRUCTLOG = "import structlog\nlog = structlog.get_logger()\n"
_ARRIVED = "turn.arrived"


_EVENT_CASES = [
    case("stdlib literal is clean", STDLIB + f'logger.info("{_ARRIVED}")\n'),
    case("structlog literal is clean", _STRUCTLOG + f'log.info("{_ARRIVED}", turn_id=3)\n'),
    case("logging module itself", 'import logging\nlogging.warning("turn.arrived")\n'),
    case(
        "logging.log has the event second",
        STDLIB + 'logger.log(20, "turn.arrived")\n',
    ),
    case(
        "logging.log with a message as event",
        STDLIB + 'logger.log(20, "Turn ended")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    case("f-string event", STDLIB + 'logger.info(f"turn {1}.ended")\n', Rule.EVENT_NOT_LITERAL),
    case("variable event", STDLIB + "logger.info(message)\n", Rule.EVENT_NOT_LITERAL),
    case(
        "formatted event",
        STDLIB + 'logger.info("turn %s.ended" % 1)\n',
        Rule.EVENT_NOT_LITERAL,
    ),
    case("byte event", STDLIB + 'logger.info(b"turn.arrived")\n', Rule.EVENT_NOT_LITERAL),
    case("starred event", STDLIB + "logger.info(*parts)\n", Rule.EVENT_NOT_LITERAL),
    case("call without an event", STDLIB + "logger.info()\n"),
    case("message as event", STDLIB + 'logger.info("Turn ended")\n', Rule.EVENT_NAME_SHAPE),
    case("service prefix", STDLIB + 'logger.error("a.b.c")\n', Rule.EVENT_NAME_SHAPE),
    case("exception call", STDLIB + 'logger.exception("turn.failed")\n'),
    case("critical call", STDLIB + 'logger.critical("process.crashed")\n'),
    case("not a logging method", STDLIB + "logger.setLevel(10)\n"),
    case("not a logger", 'import json\njson.info("anything")\n'),
    case("unknown receiver", 'handed_in.info("Not an event")\n'),
    case("call on an expression", 'make().info("Not an event")\n'),
    case("call on a subscript", 'loggers[0].info("Not an event")\n'),
]

_BINDING_CASES = [
    case(
        "aliased import",
        'import logging as lg\nlog = lg.getLogger("x")\nlog.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    case(
        "from import with alias",
        'from logging import getLogger as get\nlog = get("x")\nlog.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    case(
        "from structlog",
        'from structlog import get_logger\nlog = get_logger()\nlog.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    case(
        "structlog.stdlib.get_logger",
        'import structlog.stdlib\nlog = structlog.stdlib.get_logger()\nlog.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    case(
        "bind result is a logger",
        _STRUCTLOG + 'bound = log.bind(order_id=1)\nbound.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    case(
        "bind in a chain",
        _STRUCTLOG + 'log.bind(order_id=1).info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    case(
        "factory call as receiver",
        'import logging\nlogging.getLogger("x").info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    case(
        "annotated parameter",
        'import logging\n\ndef run(log: logging.Logger):\n    log.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    case(
        "optional annotated parameter",
        'import logging\n\ndef run(log: logging.Logger | None):\n    log.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    case(
        "structlog annotated parameter",
        'import structlog\n\ndef run(log: structlog.stdlib.BoundLogger):\n    log.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    case(
        "annotated attribute through self",
        "import logging\n\nclass Worker:\n    log: logging.Logger\n\n"
        '    def run(self):\n        self.log.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    case(
        "annotated assignment from a factory",
        'import logging\nlog: object = logging.getLogger("x")\nlog.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    case(
        "assignment on self",
        "import logging\n\nclass Worker:\n    def __init__(self):\n"
        '        self._log = logging.getLogger("x")\n\n'
        '    def run(self):\n        self._log.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    case(
        "annotation of another type",
        'def run(log: object, count: int):\n    log.info("Bad")\n',
    ),
    case(
        "string annotation is not read",
        'def run(log: "logging.Logger"):\n    log.info("Bad")\n',
    ),
    case(
        "name that is not a factory call",
        'log = make()\nlog.info("Bad")\nother = 3\n',
    ),
    case("relative import is not resolved", "from .x import getLogger\nlog = getLogger()\n"),
    case(
        "with item that is no logger",
        'with open("x") as handle, lock:\n    handle.info("Bad")\n',
    ),
    case("unannotated parameter", 'def run(log):\n    log.info("Bad")\n'),
]

_HOP_CASES = [
    case(
        "parameter logged twice with a bad caller",
        STDLIB
        + 'def note(event):\n    logger.info(event)\n    logger.warning(event)\n\nnote("Bad")\n',
        Rule.EVENT_NOT_LITERAL,
        Rule.EVENT_NOT_LITERAL,
    ),
    case(
        "parameter with literal callers",
        STDLIB
        + 'def note(event):\n    logger.info(event)\n\nnote("turn.arrived")\nnote("turn.failed")\n',
    ),
    case(
        "parameter passed by keyword",
        STDLIB + 'def note(*, event):\n    logger.info(event)\n\nnote(event="turn.arrived")\n',
    ),
    case(
        "parameter passed by position beside another keyword",
        STDLIB
        + 'def note(event, count=0):\n    logger.info(event)\n\nnote("turn.arrived", count=2)\n',
    ),
    case(
        "positional parameter passed by keyword",
        STDLIB + 'def note(event):\n    logger.info(event)\n\nnote(event="turn.arrived")\n',
    ),
    case(
        "positional parameter passed by keyword and position",
        STDLIB + 'def note(event):\n    logger.info(event)\n\nnote("Bad", event="turn.arrived")\n',
    ),
    case(
        "parameter passed a message by keyword",
        STDLIB + 'def note(event):\n    logger.info(event)\n\nnote(event="Turn ended")\n',
        Rule.EVENT_NOT_LITERAL,
    ),
    case(
        "positional-only parameter",
        STDLIB + 'def note(event, /):\n    logger.info(event)\n\nnote("turn.arrived")\n',
    ),
    case(
        "method called through self",
        "import logging\n\nclass Worker:\n"
        '    log = logging.getLogger("x")\n\n'
        "    def note(self, event):\n        self.log.info(event)\n\n"
        '    def run(self):\n        self.note("turn.arrived")\n',
    ),
    case(
        "one caller passes a message",
        STDLIB
        + 'def note(event):\n    logger.info(event)\n\nnote("turn.arrived")\nnote("Turn ended")\n',
        Rule.EVENT_NOT_LITERAL,
    ),
    case(
        "one caller passes a variable",
        STDLIB + "def note(event):\n    logger.info(event)\n\nnote(name)\n",
        Rule.EVENT_NOT_LITERAL,
    ),
    case(
        "one caller passes a starred list",
        STDLIB + "def note(event):\n    logger.info(event)\n\nnote(*names)\n",
        Rule.EVENT_NOT_LITERAL,
    ),
    case(
        "one caller leaves it out",
        STDLIB + "def note(event=None):\n    logger.info(event)\n\nnote()\n",
        Rule.EVENT_NOT_LITERAL,
    ),
    case(
        "keyword-only parameter given by position",
        STDLIB + "def note(*, event):\n    logger.info(event)\n\nnote(name)\n",
        Rule.EVENT_NOT_LITERAL,
    ),
    case(
        "no caller in the file",
        STDLIB + "def note(event):\n    logger.info(event)\n",
        Rule.EVENT_NOT_LITERAL,
    ),
    case(
        "name that is no parameter",
        STDLIB + "def note(other):\n    logger.info(event)\n\nnote(1)\n",
        Rule.EVENT_NOT_LITERAL,
    ),
    case("name outside a function", STDLIB + "logger.info(event)\n", Rule.EVENT_NOT_LITERAL),
    case(
        "method called through its class",
        "import logging\n\nclass Worker:\n"
        '    log = logging.getLogger("x")\n\n'
        "    def note(self, event):\n        self.log.info(event)\n\n"
        '    def run(self):\n        Worker.note(self, "turn.arrived")\n',
        Rule.EVENT_NOT_LITERAL,
    ),
    case(
        "async function",
        STDLIB + 'async def note(event):\n    logger.info(event)\n\nnote("turn.arrived")\n',
    ),
]

_MESSAGE_ARGUMENT_CASES = [
    case("stdlib argument", STDLIB + 'logger.info("turn.arrived", count)\n', Rule.MESSAGE_ARGS),
    case(
        "stdlib argument of log",
        STDLIB + 'logger.log(20, "turn.arrived", count)\n',
        Rule.MESSAGE_ARGS,
    ),
    case("stdlib without arguments", STDLIB + 'logger.info("turn.arrived", exc_info=True)\n'),
    case("structlog argument", _STRUCTLOG + 'log.info("turn.arrived", count)\n'),
]

_DENIED_FIELD_CASES = [
    case("structlog keyword", _STRUCTLOG + 'log.info("a.b", user_id=1)\n', Rule.DENIED_FIELD),
    case("denied suffix", _STRUCTLOG + 'log.info("a.b", wait_seconds=1)\n', Rule.DENIED_FIELD),
    case(
        "two denied keywords",
        _STRUCTLOG + 'log.info("a.b", url=1, token=2)\n',
        Rule.DENIED_FIELD,
        Rule.DENIED_FIELD,
    ),
    case("allowed keyword", _STRUCTLOG + 'log.info("a.b", duration_ms=1.5, exc_info=True)\n'),
    case("splatted fields are not read", _STRUCTLOG + 'log.info("a.b", **fields)\n'),
    case("bind keyword", _STRUCTLOG + "log.bind(path=1)\n", Rule.DENIED_FIELD),
    case("bind allowed keyword", _STRUCTLOG + "log.bind(order_id=1)\n"),
    case(
        "bind_contextvars keyword",
        "from structlog.contextvars import bind_contextvars\nbind_contextvars(prompt=1)\n",
        Rule.DENIED_FIELD,
    ),
    case(
        "extra literal key",
        STDLIB + 'logger.info("a.b", extra={"error": 1, "order_id": 2})\n',
        Rule.DENIED_FIELD,
    ),
    case("extra splat key", STDLIB + 'logger.info("a.b", extra={**fields})\n'),
    case("extra non-text key", STDLIB + 'logger.info("a.b", extra={1: 2})\n'),
    case("extra variable is not read", STDLIB + 'logger.info("a.b", extra=fields)\n'),
    case(
        "stdlib keyword other than extra",
        STDLIB + 'logger.info("a.b", stacklevel=2)\n',
    ),
    case(
        "StepTimer keyword",
        'from logline import StepTimer\nimport logging\nlog = logging.getLogger("x")\n'
        'StepTimer("turn.ended", log, subject_id=1)\n',
        Rule.DENIED_FIELD,
    ),
    case(
        "StepTimer clean",
        'from logline.step import StepTimer\nimport logging\nlog = logging.getLogger("x")\n'
        'StepTimer("turn.ended", log, turn_id=1, started_at=0.5)\n',
    ),
    case(
        "StepTimer event is a variable",
        "from logline import StepTimer\nStepTimer(name, log)\n",
        Rule.EVENT_NOT_LITERAL,
    ),
    case(
        "StepTimer event with a shape error",
        'from logline import StepTimer\nStepTimer("Turn", log)\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    case("StepTimer without arguments", "from logline import StepTimer\nStepTimer()\n"),
    case(
        "step.add keyword",
        'from logline import StepTimer\nwith StepTimer("a.ended", log) as step:\n'
        "    step.add(text=1)\n",
        Rule.DENIED_FIELD,
    ),
    case(
        "step.add on an assigned timer",
        'from logline import StepTimer\nstep = StepTimer("a.ended", log)\nstep.add(text=1)\n',
        Rule.DENIED_FIELD,
    ),
    case(
        "step.add on a timer expression",
        'from logline import StepTimer\nStepTimer("a.ended", log).add(text=1)\n',
        Rule.DENIED_FIELD,
    ),
    case(
        "step.add on an annotated timer",
        'from logline import StepTimer\nstep: StepTimer = StepTimer("a.ended", log)\n'
        "step.add(text=1)\n",
        Rule.DENIED_FIELD,
    ),
    case("add on something else", "seen = set()\nseen.add(text=1)\n"),
    case(
        "step.add clean",
        'from logline import StepTimer\nwith StepTimer("a.ended", log) as step:\n'
        "    step.add(turn_id=1)\n",
    ),
]

_ENDED = Rule.HAND_WRITTEN_ENDED

_HAND_WRITTEN_ENDED_CASES = [
    case("stdlib ended literal", STDLIB + 'logger.info("turn.ended")\n', _ENDED),
    case("structlog ended literal", _STRUCTLOG + 'log.info("turn.ended", turn_id=1)\n', _ENDED),
    case("logging.log ended literal", STDLIB + 'logger.log(20, "turn.ended")\n', _ENDED),
    case("other verb is clean", STDLIB + 'logger.info("turn.failed")\n'),
    case("suffix inside the name is clean", STDLIB + 'logger.info("ended.turn")\n'),
    case(
        "misshapen ended name is only a shape row",
        STDLIB + 'logger.info("Turn.ended")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    case(
        "ended parameter one hop away",
        STDLIB + 'def note(event):\n    logger.info(event)\n\nnote("turn.ended")\n',
        _ENDED,
    ),
    case(
        "ended parameter passed by keyword",
        STDLIB + 'def note(*, event):\n    logger.info(event)\n\nnote(event="turn.ended")\n',
        _ENDED,
    ),
    case(
        "ended parameter of a method",
        STDLIB + "class Worker:\n    def note(self, event):\n        logger.info(event)\n"
        '    def run(self):\n        self.note("turn.ended")\n',
        _ENDED,
    ),
    case(
        "parameter without an ended caller is clean",
        STDLIB + 'def note(event):\n    logger.info(event)\n\nnote("turn.failed")\n',
    ),
    case(
        "keyword-only parameter without an ended caller is clean",
        STDLIB + 'def note(*, event):\n    logger.info(event)\n\nnote(event="turn.failed")\n',
    ),
    case(
        "StepTimer event is not a logging call",
        'from logline import StepTimer\nimport logging\nlog = logging.getLogger("x")\n'
        'StepTimer("turn.ended", log)\n',
    ),
    case(
        "host wrapper around StepTimer is not a logging call",
        "from logline import StepTimer\nclass Host:\n    def _step_timer(self, event, log):\n"
        "        return StepTimer(event, log)\n\n    def run(self, log):\n"
        '        self._step_timer("lease_renewal.ended", log)\n',
    ),
]

_OWN_SETUP_CASES = [
    case("basicConfig", "import logging\nlogging.basicConfig()\n", Rule.OWN_SETUP),
    case(
        "dictConfig through import logging.config",
        "import logging.config\nlogging.config.dictConfig({})\n",
        Rule.OWN_SETUP,
    ),
    case(
        "fileConfig through from import",
        'from logging.config import fileConfig\nfileConfig("x.ini")\n',
        Rule.OWN_SETUP,
    ),
    case(
        "listen through a module alias",
        "from logging import config\nconfig.listen()\n",
        Rule.OWN_SETUP,
    ),
    case("captureWarnings", "import logging\nlogging.captureWarnings(True)\n", Rule.OWN_SETUP),
    case("structlog.configure", "import structlog\nstructlog.configure()\n", Rule.OWN_SETUP),
    case("structlog.configure_once", "import structlog as s\ns.configure_once()\n", Rule.OWN_SETUP),
    case("sys.excepthook", "import sys\nsys.excepthook = handler\n", Rule.OWN_SETUP),
    case("sys.unraisablehook", "import sys\nsys.unraisablehook = handler\n", Rule.OWN_SETUP),
    case(
        "threading.excepthook in a chain",
        "import threading\nthreading.excepthook = other = handler\n",
        Rule.OWN_SETUP,
    ),
    case("assignment to another name", "import sys\nsys.exit = handler\nx = y = 1\n"),
    case("tuple target", "import sys\nsys.argv, flag = 1, 2\n"),
    case(
        "addFilter on a named library logger",
        'import logging\nlogging.getLogger("httpx").addFilter(f)\n',
    ),
    case(
        "setLevel and addHandler on a named library logger",
        'import logging\nlib = logging.getLogger("httpx")\nlib.setLevel(30)\nlib.addHandler(h)\n',
    ),
]

_UVICORN_CASES = [
    case("run clean", "import uvicorn\nuvicorn.run(app, log_config=None)\n"),
    case("Config clean", "import uvicorn\nuvicorn.Config(app, log_config=None)\n"),
    case("main.run clean", "import uvicorn.main\nuvicorn.main.run(app, log_config=None)\n"),
    case(
        "main.run without log_config",
        "import uvicorn.main\nuvicorn.main.run(app)\n",
        Rule.UVICORN_LOG_CONFIG,
    ),
    case(
        "positional options hide log_config",
        'import uvicorn\nuvicorn.run(app, "h", 1, None)\n',
        Rule.UVICORN_OPTIONS_HIDDEN,
    ),
    case(
        "starred positional hides options",
        "import uvicorn\nuvicorn.run(*arguments)\n",
        Rule.UVICORN_OPTIONS_HIDDEN,
    ),
    case(
        "from import",
        "from uvicorn import run\nrun(app, log_config=None)\n",
    ),
    case("log_config missing", "import uvicorn\nuvicorn.run(app)\n", Rule.UVICORN_LOG_CONFIG),
    case(
        "log_config given",
        "import uvicorn\nuvicorn.run(app, log_config=LOGGING)\n",
        Rule.UVICORN_LOG_CONFIG,
    ),
    case(
        "log_config false",
        "import uvicorn\nuvicorn.Config(app, log_config=False)\n",
        Rule.UVICORN_LOG_CONFIG,
    ),
    case(
        "log_level given",
        'import uvicorn\nuvicorn.run(app, log_config=None, log_level="info")\n',
        Rule.UVICORN_LOG_LEVEL,
    ),
    case(
        "options hidden behind a splat",
        "import uvicorn\nuvicorn.run(app, **options)\n",
        Rule.UVICORN_OPTIONS_HIDDEN,
    ),
    case(
        "splat beside a clean log_config",
        "import uvicorn\nuvicorn.run(app, log_config=None, **options)\n",
        Rule.UVICORN_OPTIONS_HIDDEN,
    ),
    case(
        "splat beside a wrong log_config",
        "import uvicorn\nuvicorn.run(app, log_config=LOGGING, **options)\n",
        Rule.UVICORN_OPTIONS_HIDDEN,
        Rule.UVICORN_LOG_CONFIG,
    ),
    case("another module's run", "import asyncio\nasyncio.run(main())\n"),
    case("a call on an expression", "make()(app)\n"),
]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        *_EVENT_CASES,
        *_BINDING_CASES,
        *_HOP_CASES,
        *_MESSAGE_ARGUMENT_CASES,
        *_DENIED_FIELD_CASES,
        *_HAND_WRITTEN_ENDED_CASES,
        *_OWN_SETUP_CASES,
        *_UVICORN_CASES,
    ],
)
def test_a_source_breaks_exactly_the_rows_its_rule_names(source: str, expected: list[Rule]) -> None:
    assert rules(source) == expected


def _lines_executed_checking(source: str) -> int:
    """Count the Python lines `check` runs for `source`: a work measure with no clock in it."""
    tool = sys.monitoring.PROFILER_ID
    executed = 0

    def count_line(code: object, line_number: int) -> None:
        nonlocal executed
        executed += 1

    sys.monitoring.use_tool_id(tool, "linear-time-test")
    try:
        sys.monitoring.register_callback(tool, sys.monitoring.events.LINE, count_line)
        sys.monitoring.set_events(tool, sys.monitoring.events.LINE)
        check(source, PurePath("host.py"))
    finally:
        sys.monitoring.set_events(tool, 0)
        sys.monitoring.register_callback(tool, sys.monitoring.events.LINE, None)
        sys.monitoring.free_tool_id(tool)
    return executed


def _assert_work_grows_linearly(source_of_size: Callable[[int], str]) -> None:
    base = 250
    assert not check(source_of_size(base), PurePath("host.py"))

    work = _lines_executed_checking(source_of_size(base))
    doubled_work = _lines_executed_checking(source_of_size(2 * base))

    # Doubling the input doubles linear work and quadruples quadratic work; 3 sits between them.
    assert doubled_work < 3 * work


@pytest.mark.parametrize(
    "definition",
    [
        "def note(event):\n    logger.info(event)\n",
        "def note(event):\n    logger.info(event)\n    logger.warning(event)\n",
    ],
    ids=["one function logged once", "one function logged twice"],
)
def test_a_crafted_file_of_repeated_functions_is_checked_in_linear_time(definition: str) -> None:
    def source_of(repeats: int) -> str:
        return STDLIB + (definition + 'note("job.done")\n') * repeats

    _assert_work_grows_linearly(source_of)


def test_alike_functions_with_distinct_parameters_are_checked_in_linear_time() -> None:
    def source_of(repeats: int) -> str:
        definitions = "".join(f"def note(e{n}):\n    logger.info(e{n})\n" for n in range(repeats))
        return STDLIB + definitions + 'note("job.done")\n' * repeats

    _assert_work_grows_linearly(source_of)


def test_a_finding_names_its_path_and_the_place_of_the_node() -> None:
    source = STDLIB + '\nlogger.info("Bad")\n'

    findings = check(source, PurePath("src/app/main.py"))

    assert findings == (Finding(PurePath("src/app/main.py"), 4, 13, Rule.EVENT_NAME_SHAPE),)


def test_findings_come_in_source_order() -> None:
    source = "import uvicorn\nuvicorn.run(app)\n" + STDLIB + 'logger.info("Bad")\n'

    assert rules(source) == [Rule.UVICORN_LOG_CONFIG, Rule.EVENT_NAME_SHAPE]


def test_a_source_that_does_not_parse_raises_a_syntax_error() -> None:
    with pytest.raises(SyntaxError):
        check("def broken(:\n", PurePath("broken.py"))


@pytest.mark.parametrize(
    ("rule", "code"),
    [
        (Rule.EVENT_NOT_LITERAL, "event_not_literal"),
        (Rule.EVENT_NAME_SHAPE, "event_name_shape"),
        (Rule.MESSAGE_ARGS, "message_args"),
        (Rule.DENIED_FIELD, "denied_field"),
        (Rule.OWN_SETUP, "own_setup"),
        (Rule.UVICORN_LOG_CONFIG, "uvicorn_log_config"),
        (Rule.UVICORN_LOG_LEVEL, "uvicorn_log_level"),
        (Rule.UVICORN_OPTIONS_HIDDEN, "uvicorn_options_hidden"),
        (Rule.HAND_WRITTEN_ENDED, "hand_written_ended"),
    ],
)
def test_a_rule_code_is_its_text(rule: Rule, code: str) -> None:
    assert str(rule) == code


def test_column_counts_characters_not_bytes() -> None:
    source = 'x = "\u00e9"; log.info("Bad")\n'
    findings = check(
        "import structlog\nlog = structlog.get_logger()\n" + source, PurePath("host.py")
    )
    assert [finding.column for finding in findings] == [19]


def test_check_imports_only_the_standard_library_and_logline_standard() -> None:
    module = Path(check_module.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(module)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    foreign = {
        name
        for name in imported
        if name.split(".")[0] not in sys.stdlib_module_names and name != "logline.standard"
    }
    assert foreign == set()
