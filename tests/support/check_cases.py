from pathlib import PurePath

import pytest

from logline.check import Rule, check

STDLIB = "import logging\nlogger = logging.getLogger(__name__)\n"


def rules(source: str) -> list[Rule]:
    return [finding.rule for finding in check(source, PurePath("host.py"))]


def case(case_id: str, source: str, *expected: Rule) -> object:
    return pytest.param(source, list(expected), id=case_id)
