"""The outcome vocabulary, the clock a duration is read on, and the timer of one step.

A step is one finished piece of a unit of work: an outbound call, a wait, a compute stage. Its
`.ended` line is written by `StepTimer` through the standard-library logger the caller names, so a
host that never called `configure` gets an ordinary record under its own logging setup.
"""

import logging
import time
from enum import StrEnum
from types import TracebackType

__all__ = ["Outcome", "StepTimer"]

type Scalar = str | int | float | bool | None

_TIMER_KEYS = frozenset({"duration_ms", "outcome", "reason", "first_output_ms", "error_type"})


class Outcome(StrEnum):
    """How a step or request ended: the one owner of the `outcome` values."""

    OK = "ok"
    REFUSED = "refused"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Stopwatch:
    """The monotonic clock and rounding of every `duration_ms` and `first_output_ms`."""

    def __init__(self) -> None:
        self._started = time.perf_counter()

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._started) * 1000, 3)


class StepTimer:
    """Time one step and write its single `.ended` line when the block ends, whatever happened.

    Works as `with` and as `async with`; the line is written once, in the thread or task the block
    runs in, so it carries that context's `trace_id`. The timer starts when it is constructed.

    - `event` is the literal `<noun>.ended` name and `fields` are the step's id fields. The timer
      owns `duration_ms`, `outcome`, `reason`, `first_output_ms` and `error_type`: a field of one of
      these names is dropped, and the line carries them only as the timer sets them.
    - A block that ends normally is `ok`, or `refused` after `refuse`. An `Exception` makes it
      `failed` with `error_type` (the class name; never the message or a traceback) and propagates
      unchanged. Any other `BaseException`, a cancellation or `GeneratorExit`, makes it `cancelled`,
      without `error_type`; the exception propagates too.
    - The line is at INFO. `degraded` makes it WARNING whatever the outcome. A failed step is at
      ERROR, or WARNING when `handled` says its unit goes on.
    """

    def __init__(self, event: str, logger: logging.Logger, /, **fields: Scalar) -> None:
        self._event = event
        self._logger = logger
        self._fields = {key: value for key, value in fields.items() if key not in _TIMER_KEYS}
        self._stopwatch = Stopwatch()
        self._degraded = False
        self._handled = False
        self._refusal_reason: str | None = None
        self._first_output_ms: float | None = None

    def degraded(self) -> None:
        """The step worked but not as designed, such as a call the unit retried."""
        self._degraded = True

    def handled(self) -> None:
        """If the step fails, its unit goes on: the failed line is a warning, not an error."""
        self._handled = True

    def refuse(self, reason: str) -> None:
        """End the step as `refused` for `reason`, a code from the caller's closed set."""
        self._refusal_reason = reason

    def first_output(self) -> None:
        """Mark the first output of a streaming step; only the first call counts."""
        if self._first_output_ms is None:
            self._first_output_ms = self._stopwatch.elapsed_ms()

    def __enter__(self) -> StepTimer:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._write_line(exc)

    async def __aenter__(self) -> StepTimer:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._write_line(exc)

    def _write_line(self, exc: BaseException | None) -> None:
        outcome = self._outcome(exc)
        fields: dict[str, Scalar] = {
            **self._fields,
            "duration_ms": self._stopwatch.elapsed_ms(),
            "outcome": outcome,
        }
        if self._first_output_ms is not None:
            fields["first_output_ms"] = self._first_output_ms
        if outcome is Outcome.REFUSED:
            fields["reason"] = self._refusal_reason
        if outcome is Outcome.FAILED:
            fields["error_type"] = type(exc).__name__
        self._logger.log(self._level(outcome), self._event, extra=fields)

    def _outcome(self, exc: BaseException | None) -> Outcome:
        if exc is None:
            return Outcome.OK if self._refusal_reason is None else Outcome.REFUSED
        return Outcome.FAILED if isinstance(exc, Exception) else Outcome.CANCELLED

    def _level(self, outcome: Outcome) -> int:
        if outcome is Outcome.FAILED and not self._handled:
            return logging.ERROR
        if self._degraded or outcome is Outcome.FAILED:
            return logging.WARNING
        return logging.INFO
