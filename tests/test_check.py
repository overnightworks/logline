import ast
import sys
import time
from pathlib import Path, PurePath

import pytest

import logline.check as check_module
from logline.check import Finding, Rule, check

_STDLIB = "import logging\nlogger = logging.getLogger(__name__)\n"
_STRUCTLOG = "import structlog\nlog = structlog.get_logger()\n"
_ARRIVED = "turn.arrived"


def _rules(source: str) -> list[Rule]:
    return [finding.rule for finding in check(source, PurePath("host.py"))]


def _case(case_id: str, source: str, *expected: Rule) -> object:
    return pytest.param(source, list(expected), id=case_id)


_EVENT_CASES = [
    _case("stdlib literal is clean", _STDLIB + f'logger.info("{_ARRIVED}")\n'),
    _case("structlog literal is clean", _STRUCTLOG + f'log.info("{_ARRIVED}", turn_id=3)\n'),
    _case("logging module itself", 'import logging\nlogging.warning("turn.arrived")\n'),
    _case(
        "logging.log has the event second",
        _STDLIB + 'logger.log(20, "turn.arrived")\n',
    ),
    _case(
        "logging.log with a message as event",
        _STDLIB + 'logger.log(20, "Turn ended")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    _case("f-string event", _STDLIB + 'logger.info(f"turn {1}.ended")\n', Rule.EVENT_NOT_LITERAL),
    _case("variable event", _STDLIB + "logger.info(message)\n", Rule.EVENT_NOT_LITERAL),
    _case(
        "formatted event",
        _STDLIB + 'logger.info("turn %s.ended" % 1)\n',
        Rule.EVENT_NOT_LITERAL,
    ),
    _case("byte event", _STDLIB + 'logger.info(b"turn.arrived")\n', Rule.EVENT_NOT_LITERAL),
    _case("starred event", _STDLIB + "logger.info(*parts)\n", Rule.EVENT_NOT_LITERAL),
    _case("call without an event", _STDLIB + "logger.info()\n"),
    _case("message as event", _STDLIB + 'logger.info("Turn ended")\n', Rule.EVENT_NAME_SHAPE),
    _case("service prefix", _STDLIB + 'logger.error("a.b.c")\n', Rule.EVENT_NAME_SHAPE),
    _case("exception call", _STDLIB + 'logger.exception("turn.failed")\n'),
    _case("critical call", _STDLIB + 'logger.critical("process.crashed")\n'),
    _case("not a logging method", _STDLIB + "logger.setLevel(10)\n"),
    _case("not a logger", 'import json\njson.info("anything")\n'),
    _case("unknown receiver", 'handed_in.info("Not an event")\n'),
    _case("call on an expression", 'make().info("Not an event")\n'),
    _case("call on a subscript", 'loggers[0].info("Not an event")\n'),
]

_BINDING_CASES = [
    _case(
        "aliased import",
        'import logging as lg\nlog = lg.getLogger("x")\nlog.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    _case(
        "from import with alias",
        'from logging import getLogger as get\nlog = get("x")\nlog.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    _case(
        "from structlog",
        'from structlog import get_logger\nlog = get_logger()\nlog.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    _case(
        "structlog.stdlib.get_logger",
        'import structlog.stdlib\nlog = structlog.stdlib.get_logger()\nlog.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    _case(
        "bind result is a logger",
        _STRUCTLOG + 'bound = log.bind(order_id=1)\nbound.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    _case(
        "bind in a chain",
        _STRUCTLOG + 'log.bind(order_id=1).info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    _case(
        "factory call as receiver",
        'import logging\nlogging.getLogger("x").info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    _case(
        "annotated parameter",
        'import logging\n\ndef run(log: logging.Logger):\n    log.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    _case(
        "optional annotated parameter",
        'import logging\n\ndef run(log: logging.Logger | None):\n    log.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    _case(
        "structlog annotated parameter",
        'import structlog\n\ndef run(log: structlog.stdlib.BoundLogger):\n    log.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    _case(
        "annotated attribute through self",
        "import logging\n\nclass Worker:\n    log: logging.Logger\n\n"
        '    def run(self):\n        self.log.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    _case(
        "annotated assignment from a factory",
        'import logging\nlog: object = logging.getLogger("x")\nlog.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    _case(
        "assignment on self",
        "import logging\n\nclass Worker:\n    def __init__(self):\n"
        '        self._log = logging.getLogger("x")\n\n'
        '    def run(self):\n        self._log.info("Bad")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    _case(
        "annotation of another type",
        'def run(log: object, count: int):\n    log.info("Bad")\n',
    ),
    _case(
        "string annotation is not read",
        'def run(log: "logging.Logger"):\n    log.info("Bad")\n',
    ),
    _case(
        "name that is not a factory call",
        'log = make()\nlog.info("Bad")\nother = 3\n',
    ),
    _case("relative import is not resolved", "from .x import getLogger\nlog = getLogger()\n"),
    _case(
        "with item that is no logger",
        'with open("x") as handle, lock:\n    handle.info("Bad")\n',
    ),
    _case("unannotated parameter", 'def run(log):\n    log.info("Bad")\n'),
]

_HOP_CASES = [
    _case(
        "parameter logged twice with a bad caller",
        _STDLIB
        + 'def note(event):\n    logger.info(event)\n    logger.warning(event)\n\nnote("Bad")\n',
        Rule.EVENT_NOT_LITERAL,
        Rule.EVENT_NOT_LITERAL,
    ),
    _case(
        "parameter with literal callers",
        _STDLIB
        + 'def note(event):\n    logger.info(event)\n\nnote("turn.arrived")\nnote("turn.failed")\n',
    ),
    _case(
        "parameter passed by keyword",
        _STDLIB + 'def note(*, event):\n    logger.info(event)\n\nnote(event="turn.arrived")\n',
    ),
    _case(
        "parameter passed by position beside another keyword",
        _STDLIB
        + 'def note(event, count=0):\n    logger.info(event)\n\nnote("turn.arrived", count=2)\n',
    ),
    _case(
        "positional parameter passed by keyword",
        _STDLIB + 'def note(event):\n    logger.info(event)\n\nnote(event="turn.arrived")\n',
    ),
    _case(
        "positional parameter passed by keyword and position",
        _STDLIB + 'def note(event):\n    logger.info(event)\n\nnote("Bad", event="turn.arrived")\n',
    ),
    _case(
        "parameter passed a message by keyword",
        _STDLIB + 'def note(event):\n    logger.info(event)\n\nnote(event="Turn ended")\n',
        Rule.EVENT_NOT_LITERAL,
    ),
    _case(
        "positional-only parameter",
        _STDLIB + 'def note(event, /):\n    logger.info(event)\n\nnote("turn.arrived")\n',
    ),
    _case(
        "method called through self",
        "import logging\n\nclass Worker:\n"
        '    log = logging.getLogger("x")\n\n'
        "    def note(self, event):\n        self.log.info(event)\n\n"
        '    def run(self):\n        self.note("turn.arrived")\n',
    ),
    _case(
        "one caller passes a message",
        _STDLIB
        + 'def note(event):\n    logger.info(event)\n\nnote("turn.arrived")\nnote("Turn ended")\n',
        Rule.EVENT_NOT_LITERAL,
    ),
    _case(
        "one caller passes a variable",
        _STDLIB + "def note(event):\n    logger.info(event)\n\nnote(name)\n",
        Rule.EVENT_NOT_LITERAL,
    ),
    _case(
        "one caller passes a starred list",
        _STDLIB + "def note(event):\n    logger.info(event)\n\nnote(*names)\n",
        Rule.EVENT_NOT_LITERAL,
    ),
    _case(
        "one caller leaves it out",
        _STDLIB + "def note(event=None):\n    logger.info(event)\n\nnote()\n",
        Rule.EVENT_NOT_LITERAL,
    ),
    _case(
        "keyword-only parameter given by position",
        _STDLIB + "def note(*, event):\n    logger.info(event)\n\nnote(name)\n",
        Rule.EVENT_NOT_LITERAL,
    ),
    _case(
        "no caller in the file",
        _STDLIB + "def note(event):\n    logger.info(event)\n",
        Rule.EVENT_NOT_LITERAL,
    ),
    _case(
        "name that is no parameter",
        _STDLIB + "def note(other):\n    logger.info(event)\n\nnote(1)\n",
        Rule.EVENT_NOT_LITERAL,
    ),
    _case("name outside a function", _STDLIB + "logger.info(event)\n", Rule.EVENT_NOT_LITERAL),
    _case(
        "method called through its class",
        "import logging\n\nclass Worker:\n"
        '    log = logging.getLogger("x")\n\n'
        "    def note(self, event):\n        self.log.info(event)\n\n"
        '    def run(self):\n        Worker.note(self, "turn.arrived")\n',
        Rule.EVENT_NOT_LITERAL,
    ),
    _case(
        "async function",
        _STDLIB + 'async def note(event):\n    logger.info(event)\n\nnote("turn.arrived")\n',
    ),
]

_MESSAGE_ARGUMENT_CASES = [
    _case("stdlib argument", _STDLIB + 'logger.info("turn.arrived", count)\n', Rule.MESSAGE_ARGS),
    _case(
        "stdlib argument of log",
        _STDLIB + 'logger.log(20, "turn.arrived", count)\n',
        Rule.MESSAGE_ARGS,
    ),
    _case("stdlib without arguments", _STDLIB + 'logger.info("turn.arrived", exc_info=True)\n'),
    _case("structlog argument", _STRUCTLOG + 'log.info("turn.arrived", count)\n'),
]

_DENIED_FIELD_CASES = [
    _case("structlog keyword", _STRUCTLOG + 'log.info("a.b", user_id=1)\n', Rule.DENIED_FIELD),
    _case("denied suffix", _STRUCTLOG + 'log.info("a.b", wait_seconds=1)\n', Rule.DENIED_FIELD),
    _case(
        "two denied keywords",
        _STRUCTLOG + 'log.info("a.b", url=1, token=2)\n',
        Rule.DENIED_FIELD,
        Rule.DENIED_FIELD,
    ),
    _case("allowed keyword", _STRUCTLOG + 'log.info("a.b", duration_ms=1.5, exc_info=True)\n'),
    _case("splatted fields are not read", _STRUCTLOG + 'log.info("a.b", **fields)\n'),
    _case("bind keyword", _STRUCTLOG + "log.bind(path=1)\n", Rule.DENIED_FIELD),
    _case("bind allowed keyword", _STRUCTLOG + "log.bind(order_id=1)\n"),
    _case(
        "bind_contextvars keyword",
        "from structlog.contextvars import bind_contextvars\nbind_contextvars(prompt=1)\n",
        Rule.DENIED_FIELD,
    ),
    _case(
        "extra literal key",
        _STDLIB + 'logger.info("a.b", extra={"error": 1, "order_id": 2})\n',
        Rule.DENIED_FIELD,
    ),
    _case("extra splat key", _STDLIB + 'logger.info("a.b", extra={**fields})\n'),
    _case("extra non-text key", _STDLIB + 'logger.info("a.b", extra={1: 2})\n'),
    _case("extra variable is not read", _STDLIB + 'logger.info("a.b", extra=fields)\n'),
    _case(
        "stdlib keyword other than extra",
        _STDLIB + 'logger.info("a.b", stacklevel=2)\n',
    ),
    _case(
        "StepTimer keyword",
        'from logline import StepTimer\nimport logging\nlog = logging.getLogger("x")\n'
        'StepTimer("turn.ended", log, subject_id=1)\n',
        Rule.DENIED_FIELD,
    ),
    _case(
        "StepTimer clean",
        'from logline.step import StepTimer\nimport logging\nlog = logging.getLogger("x")\n'
        'StepTimer("turn.ended", log, turn_id=1, started_at=0.5)\n',
    ),
    _case(
        "StepTimer event is a variable",
        "from logline import StepTimer\nStepTimer(name, log)\n",
        Rule.EVENT_NOT_LITERAL,
    ),
    _case(
        "StepTimer event with a shape error",
        'from logline import StepTimer\nStepTimer("Turn", log)\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    _case("StepTimer without arguments", "from logline import StepTimer\nStepTimer()\n"),
    _case(
        "step.add keyword",
        'from logline import StepTimer\nwith StepTimer("a.ended", log) as step:\n'
        "    step.add(text=1)\n",
        Rule.DENIED_FIELD,
    ),
    _case(
        "step.add on an assigned timer",
        'from logline import StepTimer\nstep = StepTimer("a.ended", log)\nstep.add(text=1)\n',
        Rule.DENIED_FIELD,
    ),
    _case(
        "step.add on a timer expression",
        'from logline import StepTimer\nStepTimer("a.ended", log).add(text=1)\n',
        Rule.DENIED_FIELD,
    ),
    _case(
        "step.add on an annotated timer",
        'from logline import StepTimer\nstep: StepTimer = StepTimer("a.ended", log)\n'
        "step.add(text=1)\n",
        Rule.DENIED_FIELD,
    ),
    _case("add on something else", "seen = set()\nseen.add(text=1)\n"),
    _case(
        "step.add clean",
        'from logline import StepTimer\nwith StepTimer("a.ended", log) as step:\n'
        "    step.add(turn_id=1)\n",
    ),
]

_ENDED = Rule.HAND_WRITTEN_ENDED

_HAND_WRITTEN_ENDED_CASES = [
    _case("stdlib ended literal", _STDLIB + 'logger.info("turn.ended")\n', _ENDED),
    _case("structlog ended literal", _STRUCTLOG + 'log.info("turn.ended", turn_id=1)\n', _ENDED),
    _case("logging.log ended literal", _STDLIB + 'logger.log(20, "turn.ended")\n', _ENDED),
    _case("other verb is clean", _STDLIB + 'logger.info("turn.failed")\n'),
    _case("suffix inside the name is clean", _STDLIB + 'logger.info("ended.turn")\n'),
    _case(
        "misshapen ended name is only a shape row",
        _STDLIB + 'logger.info("Turn.ended")\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    _case(
        "ended parameter one hop away",
        _STDLIB + 'def note(event):\n    logger.info(event)\n\nnote("turn.ended")\n',
        _ENDED,
    ),
    _case(
        "ended parameter passed by keyword",
        _STDLIB + 'def note(*, event):\n    logger.info(event)\n\nnote(event="turn.ended")\n',
        _ENDED,
    ),
    _case(
        "ended parameter of a method",
        _STDLIB + "class Worker:\n    def note(self, event):\n        logger.info(event)\n"
        '    def run(self):\n        self.note("turn.ended")\n',
        _ENDED,
    ),
    _case(
        "parameter without an ended caller is clean",
        _STDLIB + 'def note(event):\n    logger.info(event)\n\nnote("turn.failed")\n',
    ),
    _case(
        "keyword-only parameter without an ended caller is clean",
        _STDLIB + 'def note(*, event):\n    logger.info(event)\n\nnote(event="turn.failed")\n',
    ),
    _case(
        "StepTimer event is not a logging call",
        'from logline import StepTimer\nimport logging\nlog = logging.getLogger("x")\n'
        'StepTimer("turn.ended", log)\n',
    ),
    _case(
        "host wrapper around StepTimer is not a logging call",
        "from logline import StepTimer\nclass Host:\n    def _step_timer(self, event, log):\n"
        "        return StepTimer(event, log)\n\n    def run(self, log):\n"
        '        self._step_timer("lease_renewal.ended", log)\n',
    ),
]

_OWN_SETUP_CASES = [
    _case("basicConfig", "import logging\nlogging.basicConfig()\n", Rule.OWN_SETUP),
    _case(
        "dictConfig through import logging.config",
        "import logging.config\nlogging.config.dictConfig({})\n",
        Rule.OWN_SETUP,
    ),
    _case(
        "fileConfig through from import",
        'from logging.config import fileConfig\nfileConfig("x.ini")\n',
        Rule.OWN_SETUP,
    ),
    _case(
        "listen through a module alias",
        "from logging import config\nconfig.listen()\n",
        Rule.OWN_SETUP,
    ),
    _case("captureWarnings", "import logging\nlogging.captureWarnings(True)\n", Rule.OWN_SETUP),
    _case("structlog.configure", "import structlog\nstructlog.configure()\n", Rule.OWN_SETUP),
    _case(
        "structlog.configure_once", "import structlog as s\ns.configure_once()\n", Rule.OWN_SETUP
    ),
    _case("sys.excepthook", "import sys\nsys.excepthook = handler\n", Rule.OWN_SETUP),
    _case(
        "threading.excepthook in a chain",
        "import threading\nthreading.excepthook = other = handler\n",
        Rule.OWN_SETUP,
    ),
    _case("assignment to another name", "import sys\nsys.exit = handler\nx = y = 1\n"),
    _case("tuple target", "import sys\nsys.argv, flag = 1, 2\n"),
    _case(
        "addFilter on a named library logger",
        'import logging\nlogging.getLogger("httpx").addFilter(f)\n',
    ),
    _case(
        "setLevel and addHandler on a named library logger",
        'import logging\nlib = logging.getLogger("httpx")\nlib.setLevel(30)\nlib.addHandler(h)\n',
    ),
]

_UVICORN_CASES = [
    _case("run clean", "import uvicorn\nuvicorn.run(app, log_config=None)\n"),
    _case("Config clean", "import uvicorn\nuvicorn.Config(app, log_config=None)\n"),
    _case("main.run clean", "import uvicorn.main\nuvicorn.main.run(app, log_config=None)\n"),
    _case(
        "main.run without log_config",
        "import uvicorn.main\nuvicorn.main.run(app)\n",
        Rule.UVICORN_LOG_CONFIG,
    ),
    _case(
        "positional options hide log_config",
        'import uvicorn\nuvicorn.run(app, "h", 1, None)\n',
        Rule.UVICORN_OPTIONS_HIDDEN,
    ),
    _case(
        "starred positional hides options",
        "import uvicorn\nuvicorn.run(*arguments)\n",
        Rule.UVICORN_OPTIONS_HIDDEN,
    ),
    _case(
        "from import",
        "from uvicorn import run\nrun(app, log_config=None)\n",
    ),
    _case("log_config missing", "import uvicorn\nuvicorn.run(app)\n", Rule.UVICORN_LOG_CONFIG),
    _case(
        "log_config given",
        "import uvicorn\nuvicorn.run(app, log_config=LOGGING)\n",
        Rule.UVICORN_LOG_CONFIG,
    ),
    _case(
        "log_config false",
        "import uvicorn\nuvicorn.Config(app, log_config=False)\n",
        Rule.UVICORN_LOG_CONFIG,
    ),
    _case(
        "log_level given",
        'import uvicorn\nuvicorn.run(app, log_config=None, log_level="info")\n',
        Rule.UVICORN_LOG_LEVEL,
    ),
    _case(
        "options hidden behind a splat",
        "import uvicorn\nuvicorn.run(app, **options)\n",
        Rule.UVICORN_OPTIONS_HIDDEN,
    ),
    _case(
        "splat beside a clean log_config",
        "import uvicorn\nuvicorn.run(app, log_config=None, **options)\n",
        Rule.UVICORN_OPTIONS_HIDDEN,
    ),
    _case(
        "splat beside a wrong log_config",
        "import uvicorn\nuvicorn.run(app, log_config=LOGGING, **options)\n",
        Rule.UVICORN_OPTIONS_HIDDEN,
        Rule.UVICORN_LOG_CONFIG,
    ),
    _case("another module's run", "import asyncio\nasyncio.run(main())\n"),
    _case("a call on an expression", "make()(app)\n"),
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
    assert _rules(source) == expected


@pytest.mark.parametrize(
    "definition",
    [
        "def note(event):\n    logger.info(event)\n",
        "def note(event):\n    logger.info(event)\n    logger.warning(event)\n",
    ],
    ids=["one function logged once", "one function logged twice"],
)
def test_a_crafted_file_of_repeated_functions_is_checked_in_linear_time(definition: str) -> None:
    repeats = 4000
    source = _STDLIB + (definition + 'note("job.done")\n') * repeats

    started = time.perf_counter()
    findings = check(source, PurePath("host.py"))
    elapsed = time.perf_counter() - started

    assert not findings
    assert elapsed < 3.0  # quadratic scanning took 15 s here; linear takes well under 1 s


def test_alike_functions_with_distinct_parameters_are_checked_in_linear_time() -> None:
    repeats = 2000
    definitions = "".join(f"def note(e{n}):\n    logger.info(e{n})\n" for n in range(repeats))
    source = _STDLIB + definitions + 'note("job.done")\n' * repeats

    started = time.perf_counter()
    findings = check(source, PurePath("host.py"))
    elapsed = time.perf_counter() - started

    assert not findings
    assert elapsed < 1.5  # quadratic scanning took 3.5 s here; linear takes well under 1 s


def test_a_finding_names_its_path_and_the_place_of_the_node() -> None:
    source = _STDLIB + '\nlogger.info("Bad")\n'

    findings = check(source, PurePath("src/app/main.py"))

    assert findings == (Finding(PurePath("src/app/main.py"), 4, 13, Rule.EVENT_NAME_SHAPE),)


def test_findings_come_in_source_order() -> None:
    source = "import uvicorn\nuvicorn.run(app)\n" + _STDLIB + 'logger.info("Bad")\n'

    assert _rules(source) == [Rule.UVICORN_LOG_CONFIG, Rule.EVENT_NAME_SHAPE]


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
