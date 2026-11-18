import ast
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


def _declared_all(module_name: str) -> list[str] | None:
    source = Path(logline.__path__[0]) / (
        "__init__.py" if module_name == "logline" else module_name.removeprefix("logline.") + ".py"
    )
    for statement in ast.parse(source.read_text(encoding="utf-8")).body:
        match statement:
            case (
                ast.Assign(targets=[ast.Name(id="__all__")], value=value)
                | ast.AnnAssign(target=ast.Name(id="__all__"), value=value)
            ) if value is not None:
                return list(ast.literal_eval(value))
    return None


_MODULE_NAMES = ["logline"] + [
    module.name for module in pkgutil.iter_modules(logline.__path__, "logline.")
]


@pytest.mark.parametrize("module_name", _MODULE_NAMES)
def test_only_a_public_module_lists_names_in_all(module_name: str) -> None:
    declared = _declared_all(module_name)

    assert declared is not None, f"{module_name} declares no __all__"
    assert bool(declared) == (module_name in _public_modules())


def test_the_readme_names_modules_that_exist() -> None:
    assert _public_modules() <= set(_MODULE_NAMES)
