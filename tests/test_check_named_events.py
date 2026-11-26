import pytest

from logline.check import Rule
from tests.support.check_cases import STDLIB, case, rules

_STEP_TIMER = "from logline import StepTimer\n"
_NOT_LITERAL = Rule.EVENT_NOT_LITERAL

_NAMED_EVENT_CASES = [
    case(
        "Final constant",
        STDLIB + 'from typing import Final\nEVENT: Final = "turn.arrived"\nlogger.info(EVENT)\n',
    ),
    case(
        "Final[str] constant",
        STDLIB
        + 'from typing import Final\nEVENT: Final[str] = "turn.arrived"\nlogger.info(EVENT)\n',
    ),
    case(
        "Final constant from typing_extensions",
        STDLIB
        + "from typing_extensions import Final\n"
        + 'EVENT: Final = "turn.arrived"\nlogger.info(EVENT)\n',
    ),
    case(
        "Final constant through an aliased module",
        STDLIB + 'import typing as t\nEVENT: t.Final = "turn.arrived"\nlogger.info(EVENT)\n',
    ),
    case(
        "Final constant through an aliased name",
        STDLIB
        + "from typing import Final as Fixed\n"
        + 'EVENT: Fixed[str] = "turn.arrived"\nlogger.info(EVENT)\n',
    ),
    case(
        "Final constant of another type",
        STDLIB + 'from typing import Final\nEVENT: Final[bytes] = b"x"\nlogger.info(EVENT)\n',
        _NOT_LITERAL,
    ),
    case(
        "Final constant at a StepTimer",
        _STEP_TIMER + 'from typing import Final\nEVENT: Final = "turn.arrived"\nStepTimer(EVENT)\n',
    ),
    case(
        "badly shaped Final constant",
        STDLIB + 'from typing import Final\nEVENT: Final = "Turn ended"\nlogger.info(EVENT)\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    case(
        "badly shaped Final constant at a StepTimer",
        _STEP_TIMER + 'from typing import Final\nEVENT: Final = "Turn ended"\nStepTimer(EVENT)\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    case(
        "ended Final constant at a logging call",
        STDLIB + 'from typing import Final\nEVENT: Final = "turn.ended"\nlogger.info(EVENT)\n',
        Rule.HAND_WRITTEN_ENDED,
    ),
    case(
        "ended Final constant at a StepTimer",
        _STEP_TIMER + 'from typing import Final\nEVENT: Final = "turn.ended"\nStepTimer(EVENT)\n',
    ),
    case(
        "StrEnum member",
        STDLIB
        + "from enum import StrEnum\n"
        + 'class Event(StrEnum):\n    ARRIVED = "turn.arrived"\n\nlogger.info(Event.ARRIVED)\n',
    ),
    case(
        "StrEnum member through an aliased import",
        STDLIB
        + "import enum as e\n"
        + 'class Event(e.StrEnum):\n    ARRIVED = "turn.arrived"\n\nlogger.info(Event.ARRIVED)\n',
    ),
    case(
        "StrEnum member beside a docstring and a method",
        STDLIB
        + "from enum import StrEnum\n"
        + 'class Event(StrEnum):\n    """Events."""\n\n    ARRIVED = "turn.arrived"\n\n'
        + "    def label(self):\n        return self\n\nlogger.info(Event.ARRIVED)\n",
    ),
    case(
        "badly shaped StrEnum member",
        STDLIB
        + "from enum import StrEnum\n"
        + 'class Event(StrEnum):\n    ARRIVED = "Arrived"\n\nlogger.info(Event.ARRIVED)\n',
        Rule.EVENT_NAME_SHAPE,
    ),
    case(
        "ended StrEnum member at a logging call",
        STDLIB
        + "from enum import StrEnum\n"
        + 'class Event(StrEnum):\n    ENDED = "turn.ended"\n\nlogger.info(Event.ENDED)\n',
        Rule.HAND_WRITTEN_ENDED,
    ),
    case(
        "StrEnum member assigned auto",
        STDLIB
        + "from enum import StrEnum, auto\n"
        + "class Event(StrEnum):\n    ARRIVED = auto()\n\nlogger.info(Event.ARRIVED)\n",
        _NOT_LITERAL,
    ),
    case(
        "member of a class that is no StrEnum",
        STDLIB
        + "from enum import Enum\n"
        + 'class Event(Enum):\n    ARRIVED = "turn.arrived"\n\nlogger.info(Event.ARRIVED)\n',
        _NOT_LITERAL,
    ),
    case(
        "member that the enum does not define",
        STDLIB
        + "from enum import StrEnum\n"
        + 'class Event(StrEnum):\n    ARRIVED = "turn.arrived"\n\nlogger.info(Event.FAILED)\n',
        _NOT_LITERAL,
    ),
    case(
        "StrEnum imported from another file",
        STDLIB + "from events import Event\nlogger.info(Event.ARRIVED)\n",
        _NOT_LITERAL,
    ),
    case(
        "Final constant imported from another file",
        STDLIB + "from events import EVENT\nlogger.info(EVENT)\n",
        _NOT_LITERAL,
    ),
    case(
        "constant bound without Final",
        STDLIB + 'EVENT = "turn.arrived"\nlogger.info(EVENT)\n',
        _NOT_LITERAL,
    ),
    case(
        "Final constant rebound by an except handler",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "turn.arrived"\n'
        + "try:\n    pass\nexcept Exception as EVENT:\n    pass\nlogger.info(EVENT)\n",
        _NOT_LITERAL,
    ),
    case(
        "Final constant rebound by a match capture",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "turn.arrived"\n'
        + "match 1:\n    case EVENT:\n        pass\nlogger.info(EVENT)\n",
        _NOT_LITERAL,
    ),
    case(
        "Final constant rebound by a match star",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "turn.arrived"\n'
        + "match [1]:\n    case [*EVENT]:\n        pass\nlogger.info(EVENT)\n",
        _NOT_LITERAL,
    ),
    case(
        "Final constant rebound by a match mapping rest",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "turn.arrived"\n'
        + "match {}:\n    case {**EVENT}:\n        pass\nlogger.info(EVENT)\n",
        _NOT_LITERAL,
    ),
    case(
        "Final constant rebound through global in another function",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "turn.arrived"\n'
        + "def swap():\n    global EVENT\n    EVENT = 1\n"
        + "def note():\n    logger.info(EVENT)\n",
        _NOT_LITERAL,
    ),
    case(
        "Final constant beside a bare except handler",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "turn.arrived"\n'
        + "try:\n    pass\nexcept Exception:\n    pass\nlogger.info(EVENT)\n",
    ),
    case(
        "Final constant beside a wildcard match",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "turn.arrived"\n'
        + "match 1:\n    case _:\n        pass\nlogger.info(EVENT)\n",
    ),
    case(
        "Final constant beside an unnamed match star",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "turn.arrived"\n'
        + "match [1]:\n    case [*_]:\n        pass\nlogger.info(EVENT)\n",
    ),
    case(
        "Final constant shadowed by an except handler in the function",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "turn.arrived"\n'
        + "def note():\n    try:\n        pass\n    except Exception as EVENT:\n"
        + "        logger.info(EVENT)\n",
        _NOT_LITERAL,
    ),
    case(
        "Final constant shadowed by a match capture in the function",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "turn.arrived"\n'
        + "def note():\n    match 1:\n        case EVENT:\n            logger.info(EVENT)\n",
        _NOT_LITERAL,
    ),
    case(
        "Final constant used beside a nested function that binds its name",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "turn.arrived"\n'
        + "def note():\n    def inner(EVENT):\n        pass\n    logger.info(EVENT)\n",
    ),
    case(
        "Final constant bound twice",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "turn.arrived"\nEVENT = "turn.failed"\n'
        + "logger.info(EVENT)\n",
        _NOT_LITERAL,
    ),
    case(
        "Final constant bound in a branch and again",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "turn.arrived"\n'
        + 'if flag:\n    EVENT = "turn.failed"\nlogger.info(EVENT)\n',
        _NOT_LITERAL,
    ),
    case(
        "Final constant shadowed by a parameter",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "turn.arrived"\n'
        + "def note(EVENT):\n    logger.info(EVENT)\n",
        _NOT_LITERAL,
    ),
    case(
        "Final constant shadowed by an assignment in the function",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "turn.arrived"\n'
        + "def note():\n    EVENT = compute()\n    logger.info(EVENT)\n",
        _NOT_LITERAL,
    ),
    case(
        "Final constant shadowed by an assignment in an outer function",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "turn.arrived"\n'
        + "def note():\n    EVENT = compute()\n    def inner():\n        logger.info(EVENT)\n",
        _NOT_LITERAL,
    ),
    case(
        "Final constant used inside an unrelated function",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "turn.arrived"\n'
        + "def note():\n    logger.info(EVENT)\n",
    ),
    case(
        "StrEnum shadowed by a parameter",
        STDLIB
        + "from enum import StrEnum\n"
        + 'class Event(StrEnum):\n    ARRIVED = "turn.arrived"\n\n'
        + "def note(Event):\n    logger.info(Event.ARRIVED)\n",
        _NOT_LITERAL,
    ),
    case(
        "parameter whose caller passes a Final constant",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "turn.arrived"\n'
        + "def note(event):\n    logger.info(event)\n\nnote(EVENT)\n",
    ),
    case(
        "parameter whose caller passes a badly shaped Final constant",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "Turn ended"\n'
        + "def note(event):\n    logger.info(event)\n\nnote(EVENT)\n",
        _NOT_LITERAL,
    ),
    case(
        "parameter whose caller passes a StrEnum member by keyword",
        STDLIB
        + "from enum import StrEnum\n"
        + 'class Event(StrEnum):\n    ARRIVED = "turn.arrived"\n\n'
        + "def note(*, event):\n    logger.info(event)\n\nnote(event=Event.ARRIVED)\n",
    ),
    case(
        "parameter whose caller passes an ended Final constant",
        STDLIB
        + 'from typing import Final\nEVENT: Final = "turn.ended"\n'
        + "def note(event):\n    logger.info(event)\n\nnote(EVENT)\n",
        Rule.HAND_WRITTEN_ENDED,
    ),
]


@pytest.mark.parametrize(("source", "expected"), _NAMED_EVENT_CASES)
def test_an_event_held_in_a_same_module_name_is_judged_as_its_literal(
    source: str, expected: list[Rule]
) -> None:
    assert rules(source) == expected
