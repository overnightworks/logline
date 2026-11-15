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

_TIMER_KEYS = frozenset(
    {"duration_ms", "outcome", "reason", "first_output_ms", "error_type", "started_at"}
)
# Logger.makeRecord raises KeyError on an extra key that names one of these record attributes.
_RECORD_ATTRIBUTES = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class Outcome(StrEnum):
    """How a step or request ended: the one owner of the `outcome` values."""

    OK = "ok"
    REFUSED = "refused"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Stopwatch:
    """The monotonic clock and rounding of every `duration_ms` and `first_output_ms`."""

    def __init__(self, started_at: float | None = None) -> None:
        now = time.perf_counter()
        if started_at is not None and started_at > now:
            raise ValueError("started_at is later than time.perf_counter()")
        self._started = now if started_at is None else started_at

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._started) * 1000, 3)


def _step_fields(fields: dict[str, Scalar]) -> dict[str, Scalar]:
    """The fields a step line may carry: a record attribute name raises, a timer key is dropped."""
    reserved = sorted(fields.keys() & _RECORD_ATTRIBUTES)
    if reserved:
        raise ValueError(f"step fields named like logging.LogRecord attributes: {reserved}")
    return {key: value for key, value in fields.items() if key not in _TIMER_KEYS}


class StepTimer:
    """Time one step and write its single `.ended` line when the block ends, whatever happened.

    Works as `with` and as `async with`; the line is written once, in the thread or task the block
    runs in, so it carries that context's `trace_id`. The timer starts when it is constructed, not
    when the block is entered, or at `started_at` when the caller names it. A step that no block
    surrounds is ended by `end` or `end_cancelled`.

    - `event` is the literal `<noun>.ended` name and `fields` are the step's id fields. The timer
      owns `duration_ms`, `outcome`, `reason`, `first_output_ms` and `error_type`: a field of one of
      these names is dropped, and the line carries them only as the timer sets them. A field named
      like an attribute of `logging.LogRecord` (`name`, `module`, `process`, `args`, ...) raises
      `ValueError`, at construction and in `add`, which sets more fields before the line is
      written; a later value for the same key replaces the earlier one.
    - `started_at` is a keyword-only `time.perf_counter()` value, for a step that began before the
      timer can be constructed: `duration_ms` and `first_output_ms` count from it. One later than
      `time.perf_counter()` raises `ValueError` at construction. It is no step field name: `add`
      drops a field of that name, and the line never carries it.
    - A block that ends normally is `ok`, or `refused` after `refuse`, or `failed` with `reason`
      after `fail`, which wins over `refuse` in either order. An `Exception` makes it `failed`
      with `error_type` (the class name; never the message or a traceback), beside the `reason`
      when `fail` named one, and propagates unchanged. Any other `BaseException`, a cancellation
      or `GeneratorExit`, makes it `cancelled`, without `error_type` and `reason`; the exception
      propagates too.
    - `end` writes the line now, as a block that ends normally would; `end_cancelled` writes it
      now, as a block that a cancellation leaves would: `cancelled`, without `error_type` and
      `reason`. The line is written once: after either or the block's exit, a later one writes
      nothing.
    - The line is at INFO. `degraded` makes it WARNING. A failed step is at ERROR, or WARNING when
      `handled` says its unit goes on; one that is degraded and failed unhandled stays at ERROR.
    """

    def __init__(
        self,
        event: str,
        logger: logging.Logger,
        /,
        *,
        started_at: float | None = None,
        **fields: Scalar,
    ) -> None:
        self._event = event
        self._logger = logger
        self._fields = _step_fields(fields)
        self._stopwatch = Stopwatch(started_at)
        self._degraded = False
        self._handled = False
        self._refusal_reason: str | None = None
        self._failure_reason: str | None = None
        self._first_output_ms: float | None = None
        self._written = False

    def add(self, **fields: Scalar) -> None:
        """Add id or count fields known only before the step ends; a later value replaces."""
        self._fields.update(_step_fields(fields))

    def degraded(self) -> None:
        """The step worked but not as designed, such as a call the unit retried."""
        self._degraded = True

    def handled(self) -> None:
        """If the step fails, its unit goes on: the failed line is a warning, not an error."""
        self._handled = True

    def refuse(self, reason: str) -> None:
        """End the step as `refused` for `reason`, a code from the caller's closed set."""
        self._refusal_reason = reason

    def fail(self, reason: str) -> None:
        """End the step as `failed` for `reason`, a code from the caller's closed set."""
        self._failure_reason = reason

    def end(self) -> None:
        """Write the line now, for a step no block surrounds; later calls and exits write none."""
        self._write_line(None)

    def end_cancelled(self) -> None:
        """Write the line now as `cancelled`, for a step without a block; later ones write none."""
        self._write_line(None, cancelled=True)

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

    def _write_line(self, exc: BaseException | None, *, cancelled: bool = False) -> None:
        if self._written:
            return
        self._written = True
        outcome = self._outcome(exc, cancelled)
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
            fields.update(self._failure_fields(exc))
        self._logger.log(self._level(outcome), self._event, extra=fields)

    def _failure_fields(self, exc: BaseException | None) -> dict[str, Scalar]:
        fields: dict[str, Scalar] = {}
        if exc is not None:
            fields["error_type"] = type(exc).__name__
        if self._failure_reason is not None:
            fields["reason"] = self._failure_reason
        return fields

    def _outcome(self, exc: BaseException | None, cancelled: bool) -> Outcome:
        if cancelled:
            return Outcome.CANCELLED
        if exc is None:
            return self._outcome_of_a_normal_end()
        return Outcome.FAILED if isinstance(exc, Exception) else Outcome.CANCELLED

    def _outcome_of_a_normal_end(self) -> Outcome:
        if self._failure_reason is not None:
            return Outcome.FAILED
        return Outcome.OK if self._refusal_reason is None else Outcome.REFUSED

    def _level(self, outcome: Outcome) -> int:
        if outcome is Outcome.FAILED and not self._handled:
            return logging.ERROR
        if self._degraded or outcome is Outcome.FAILED:
            return logging.WARNING
        return logging.INFO
