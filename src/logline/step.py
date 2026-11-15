"""The outcome vocabulary and the clock a duration is read on."""

import time
from enum import StrEnum

__all__ = ["Outcome"]


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
