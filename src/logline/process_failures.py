"""The hooks that turn a dying process, a dying thread and a Python warning into record lines."""

import logging
import sys
import threading
import warnings
from types import TracebackType
from typing import TextIO, cast

__all__: list[str] = []

_CRASH_LOGGER = logging.getLogger("logline.crash")
_WARNINGS_LOGGER = logging.getLogger("py.warnings")
PROCESS_CRASHED = "process.crashed"
THREAD_CRASHED = "thread.crashed"
PYTHON_WARNING_RAISED = "python_warning.raised"


def _write_crash(
    event: str,
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
) -> None:
    _CRASH_LOGGER.critical(
        event,
        exc_info=(exc_type, exc_value, exc_traceback),
        extra={"error_type": exc_type.__name__},
    )


def _process_crashed(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
) -> None:
    # The interpreter exits with status 1 once this returns; the default hook's text is not printed.
    _write_crash(PROCESS_CRASHED, exc_type, exc_value, exc_traceback)


def _thread_crashed(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit:
        return  # a thread that ends with `SystemExit` ends quietly, as in the default hook
    # The standard library types `exc_value` as optional; a raised thread always has one.
    exc_value = cast("BaseException", args.exc_value)
    _write_crash(THREAD_CRASHED, args.exc_type, exc_value, args.exc_traceback)


def _python_warning_raised(
    message: Warning | str,
    category: type[Warning],
    filename: str,
    lineno: int,
    file: TextIO | None = None,
    line: str | None = None,
) -> None:
    if file is not None:
        # A caller that names a stream asked for text in it, as `logging.captureWarnings` honours.
        file.write(warnings.formatwarning(message, category, filename, lineno, line))
        return
    # The message text and the source location stay out of the line.
    _WARNINGS_LOGGER.warning(PYTHON_WARNING_RAISED, extra={"error_type": category.__name__})


def install_process_failure_hooks() -> None:
    """Route an uncaught exception, a thread's uncaught exception and a warning to the logging."""
    sys.excepthook = _process_crashed
    threading.excepthook = _thread_crashed
    warnings.showwarning = _python_warning_raised
