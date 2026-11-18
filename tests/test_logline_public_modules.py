import importlib
import pkgutil
import re
from pathlib import Path

import pytest

import logline

_ROOT = Path(__file__).resolve().parent.parent
_PUBLIC_MODULES_SENTENCE = re.compile(r"The public modules are ([^;]+);")


def _public_modules() -> set[str]:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    sentence = _PUBLIC_MODULES_SENTENCE.search(readme)
    assert sentence, "README's Versioning section no longer names the public modules"
    return set(re.findall(r"`([\w.]+)`", sentence.group(1)))


_MODULE_NAMES = ["logline"] + [
    module.name for module in pkgutil.walk_packages(logline.__path__, "logline.")
]


@pytest.mark.parametrize("module_name", _MODULE_NAMES)
def test_only_a_public_module_lists_names_in_all(module_name: str) -> None:
    listed = getattr(importlib.import_module(module_name), "__all__", None)

    if module_name in _public_modules():
        assert listed, f"{module_name} is public and must list its names in __all__"
    else:
        assert not listed, f"{module_name} is not public and must not list names in __all__"


def test_the_readme_names_modules_that_exist() -> None:
    assert _public_modules() <= set(_MODULE_NAMES)
