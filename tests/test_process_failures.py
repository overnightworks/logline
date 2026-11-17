import subprocess
import sys
import threading
import warnings
from pathlib import Path

import pytest

from logline.testing import configured
from tests.support.failing_process import SECRET
from tests.support.lines import BASE_KEYS, Line, json_lines

_REPOSITORY = Path(__file__).parents[1]


def _run_failing_process(failure: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tests.support.failing_process", failure],
        cwd=_REPOSITORY,
        capture_output=True,
        text=True,
        check=False,
    )


def _the_line_of(output: str, event: str) -> Line:
    (line,) = [line for line in json_lines(output) if line["event"] == event]
    return line


@pytest.mark.parametrize(
    ("failure", "event", "level", "logger", "error_type", "exit_status"),
    [
        ("crash", "process.crashed", "critical", "logline.crash", "RuntimeError", 1),
        ("thread", "thread.crashed", "critical", "logline.crash", "RuntimeError", 0),
        ("warning", "python_warning.raised", "warning", "py.warnings", "DeprecationWarning", 0),
    ],
)
def test_a_failing_process_writes_one_record_line_on_stdout(
    failure: str,
    event: str,
    level: str,
    logger: str,
    error_type: str,
    exit_status: int,
) -> None:
    result = _run_failing_process(failure)

    line = _the_line_of(result.stdout, event)
    assert (line["level"], line["logger"], line["error_type"]) == (level, logger, error_type)
    assert BASE_KEYS <= line.keys()
    assert (result.returncode, result.stderr) == (exit_status, "")


def test_a_crash_keeps_its_message_inside_the_traceback() -> None:
    result = _run_failing_process("crash")

    line = _the_line_of(result.stdout, "process.crashed")
    assert SECRET in str(line["exception"])
    assert SECRET not in str({key: value for key, value in line.items() if key != "exception"})


def test_a_warning_line_carries_no_message_text(capsys: pytest.CaptureFixture[str]) -> None:
    with configured("warned"):
        warnings.warn(SECRET, UserWarning, stacklevel=1)

    line = _the_line_of(capsys.readouterr().out, "python_warning.raised")
    assert SECRET not in str(line)


def test_a_thread_that_exits_with_system_exit_writes_no_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with configured("exiting"):
        thread = threading.Thread(target=sys.exit)
        thread.start()
        thread.join()

    assert capsys.readouterr().out == ""


def test_the_process_hook_writes_a_critical_line_in_process(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with configured("dying"):
        try:
            raise ValueError(SECRET)
        except ValueError as error:
            sys.excepthook(type(error), error, error.__traceback__)

    line = _the_line_of(capsys.readouterr().out, "process.crashed")
    assert (line["level"], line["error_type"]) == ("critical", "ValueError")


def test_leaving_the_block_restores_the_hooks_the_host_had() -> None:
    before = (sys.excepthook, threading.excepthook, warnings.showwarning)

    with configured("restoring"):
        assert (sys.excepthook, threading.excepthook, warnings.showwarning) != before

    assert (sys.excepthook, threading.excepthook, warnings.showwarning) == before


def test_a_thread_that_raises_writes_a_critical_line_in_process(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> None:
        raise ValueError(SECRET)

    with configured("threading"):
        thread = threading.Thread(target=fail)
        thread.start()
        thread.join()

    line = _the_line_of(capsys.readouterr().out, "thread.crashed")
    assert (line["level"], line["error_type"]) == ("critical", "ValueError")


def test_a_warning_sent_to_a_named_stream_stays_text_in_that_stream() -> None:
    result = _run_failing_process("streamed_warning")

    assert SECRET in result.stderr
    assert "python_warning.raised" not in result.stdout
