import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from fastapi import FastAPI

from logline import StepTimer

STEP_LOGGER = "tests.steps"
STEP_EVENT = "gateway_call.ended"
STEP_AGENT_ID = "agent-7"
STEP_REFUSAL_REASON = "overloaded"
STEP_FAILURE_MESSAGE = "failure message that stays out of the line"


@dataclass(frozen=True)
class StepScenario:
    """What the step routes add, fail with and collide on, which the step tests read back."""

    failure_reason: str = "rate_limited"
    ok_reason: str = "silence"
    count: int = 42
    later_agent_id: str = "agent-8"
    reserved_field: str = "module"
    earlier_start_seconds: int = 5
    seconds_before_first_output: int = 1
    seconds_after_first_output: int = 2


STEP = StepScenario()


class Steps:
    """Builds the steps the routes run, on a clock that moves only when a route advances it."""

    def __init__(self) -> None:
        self._now = 1000.0

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def __call__(self, *, started_at: float | None = None, **fields: str | float) -> StepTimer:
        return _ManualStepTimer(
            self,
            STEP_EVENT,
            logging.getLogger(STEP_LOGGER),
            started_at=started_at,
            **{"agent_id": STEP_AGENT_ID, **fields},
        )


class _ManualStepTimer(StepTimer):
    def __init__(
        self,
        steps: Steps,
        event: str,
        logger: logging.Logger,
        /,
        *,
        started_at: float | None,
        **fields: str | float,
    ) -> None:
        self._steps = steps
        super().__init__(event, logger, started_at=started_at, **fields)

    def _read_clock(self) -> float:
        return self._steps.now()


type _Answer = dict[str, str | bool | None]


def _caught(caught: Exception, raised: Exception) -> _Answer:
    return {"caught": str(caught), "same": caught is raised}


def _sync_step(steps: Steps, *, failure: Exception | None) -> None:
    with steps():
        if failure is not None:
            raise failure


async def _generator_of_a_step(steps: Steps) -> AsyncGenerator[int]:
    async with steps():
        yield 1
        yield 2


def _add_step_outcome_routes(app: FastAPI, steps: Steps) -> None:
    """Routes that run one step to an outcome; the response says which exception got out."""

    @app.get("/step/ok")
    async def step_ok() -> _Answer:
        async with steps():
            pass
        return {"caught": None}

    @app.get("/step/failed")
    async def step_failed() -> _Answer:
        failure = ValueError(STEP_FAILURE_MESSAGE)
        try:
            async with steps():
                raise failure
        except ValueError as caught:
            return _caught(caught, failure)

    @app.get("/step/failed-handled")
    async def step_failed_handled() -> _Answer:
        failure = ValueError(STEP_FAILURE_MESSAGE)
        try:
            async with steps() as step:
                step.handled()
                raise failure
        except ValueError as caught:
            return _caught(caught, failure)

    @app.get("/step/failed-escaping")
    async def step_failed_escaping() -> None:
        async with steps():
            raise ValueError(STEP_FAILURE_MESSAGE)

    @app.get("/step/degraded")
    async def step_degraded() -> _Answer:
        async with steps() as step:
            step.degraded()
        return {"caught": None}

    @app.get("/step/refused")
    async def step_refused() -> _Answer:
        async with steps() as step:
            step.refuse(STEP_REFUSAL_REASON)
        return {"caught": None}

    @app.get("/step/ok-with-reason")
    async def step_ok_with_reason() -> _Answer:
        async with steps() as step:
            step.reason(STEP.ok_reason)
        return {"caught": None}


def _add_step_reason_routes(app: FastAPI, steps: Steps) -> None:
    """Routes whose step gives an ok reason and then ends otherwise."""

    @app.get("/step/reason-then-refuse")
    async def step_reason_then_refuse() -> _Answer:
        async with steps() as step:
            step.reason(STEP.ok_reason)
            step.refuse(STEP_REFUSAL_REASON)
        return {"caught": None}

    @app.get("/step/reason-then-fail")
    async def step_reason_then_fail() -> _Answer:
        async with steps() as step:
            step.reason(STEP.ok_reason)
            step.fail(STEP.failure_reason)
        return {"caught": None}

    @app.get("/step/reason-then-raise")
    async def step_reason_then_raise() -> _Answer:
        failure = ValueError(STEP_FAILURE_MESSAGE)
        try:
            async with steps() as step:
                step.reason(STEP.ok_reason)
                raise failure
        except ValueError as caught:
            return _caught(caught, failure)

    @app.get("/step/reason-then-cancel")
    async def step_reason_then_cancel() -> _Answer:
        step = steps()
        step.reason(STEP.ok_reason)
        step.end_cancelled()
        return {"caught": None}

    @app.get("/step/refused-degraded")
    async def step_refused_degraded() -> _Answer:
        async with steps() as step:
            step.degraded()
            step.refuse(STEP_REFUSAL_REASON)
        return {"caught": None}


def _add_step_lifetime_routes(app: FastAPI, steps: Steps) -> None:
    """Routes whose step is cancelled or lives in a generator."""

    @app.get("/step/cancelled")
    async def step_cancelled() -> _Answer:
        async def wait_forever() -> None:
            async with steps():
                await asyncio.Event().wait()

        waiting = asyncio.create_task(wait_forever())
        await asyncio.sleep(0)
        waiting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiting
        return {"caught": None, "cancelled": waiting.cancelled()}

    @app.get("/step/generator-closed")
    async def step_generator_closed() -> _Answer:
        generator = _generator_of_a_step(steps)
        await anext(generator)
        await generator.aclose()
        return {"caught": None}

    @app.get("/step/generator-finished")
    async def step_generator_finished() -> _Answer:
        async with contextlib.aclosing(_generator_of_a_step(steps)) as generator:
            async for _ in generator:
                pass
        return {"caught": None}


def _add_step_field_routes(app: FastAPI, steps: Steps) -> None:
    """Routes whose step times and adds fields."""

    @app.get("/step/timer-named-fields")
    async def step_timer_named_fields() -> _Answer:
        async with steps(
            duration_ms=-1,
            outcome="failed",
            reason="caller",
            first_output_ms=-1,
            error_type="Caller",
        ):
            pass
        return {"caught": None}

    @app.get("/step/first-output")
    async def step_first_output() -> _Answer:
        async with steps() as step:
            steps.advance(STEP.seconds_before_first_output)
            step.first_output()
            steps.advance(STEP.seconds_after_first_output)
            step.first_output()
        return {"caught": None}

    @app.get("/step/add")
    async def step_add() -> _Answer:
        async with steps() as step:
            step.add(input_tokens=1, output_tokens=STEP.count, duration_ms=-1, reason="caller")
            step.add(input_tokens=STEP.count, agent_id=STEP.later_agent_id, error_type="Caller")
            step.add(started_at=STEP.earlier_start_seconds)
        return {"caught": None}

    @app.get("/step/add-reserved")
    async def step_add_reserved() -> _Answer:
        try:
            async with steps() as step:
                step.add(**{STEP.reserved_field: "value"})
        except ValueError as caught:
            return {"caught": str(caught)}
        return {"caught": None}

    @app.get("/step/add-reserved-escaping")
    async def step_add_reserved_escaping() -> None:
        async with steps() as step:
            step.add(**{STEP.reserved_field: "value"})


def _add_step_failure_routes(app: FastAPI, steps: Steps) -> None:
    """Routes that fail, refuse or degrade a step, with or without a raise."""

    @app.get("/step/refuse-then-raise")
    async def step_refuse_then_raise() -> _Answer:
        failure = ValueError(STEP_FAILURE_MESSAGE)
        try:
            async with steps() as step:
                step.refuse(STEP_REFUSAL_REASON)
                raise failure
        except ValueError as caught:
            return _caught(caught, failure)

    @app.get("/step/fail")
    async def step_fail() -> _Answer:
        async with steps() as step:
            step.fail(STEP.failure_reason)
        return {"caught": None}

    @app.get("/step/degraded-then-fail")
    async def step_degraded_then_fail() -> _Answer:
        async with steps() as step:
            step.degraded()
            step.fail(STEP.failure_reason)
        return {"caught": None}

    @app.get("/step/fail-handled")
    async def step_fail_handled() -> _Answer:
        async with steps() as step:
            step.handled()
            step.fail(STEP.failure_reason)
        return {"caught": None}


def _add_step_conflict_routes(app: FastAPI, steps: Steps) -> None:
    """Routes that give a step two outcomes in turn."""

    @app.get("/step/fail-then-refuse")
    async def step_fail_then_refuse() -> _Answer:
        async with steps() as step:
            step.fail(STEP.failure_reason)
            step.refuse(STEP_REFUSAL_REASON)
        return {"caught": None}

    @app.get("/step/refuse-then-fail")
    async def step_refuse_then_fail() -> _Answer:
        async with steps() as step:
            step.refuse(STEP_REFUSAL_REASON)
            step.fail(STEP.failure_reason)
        return {"caught": None}

    @app.get("/step/fail-then-raise")
    async def step_fail_then_raise() -> _Answer:
        failure = ValueError(STEP_FAILURE_MESSAGE)
        try:
            async with steps() as step:
                step.fail(STEP.failure_reason)
                raise failure
        except ValueError as caught:
            return _caught(caught, failure)

    @app.get("/step/fail-then-cancel")
    async def step_fail_then_cancel() -> _Answer:
        async def wait_forever() -> None:
            async with steps() as step:
                step.fail(STEP.failure_reason)
                await asyncio.Event().wait()

        waiting = asyncio.create_task(wait_forever())
        await asyncio.sleep(0)
        waiting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiting
        return {"caught": None}


def _add_step_end_routes(app: FastAPI, steps: Steps) -> None:
    """Routes that end a step by hand."""

    @app.get("/step/end")
    async def step_end() -> _Answer:
        step = steps()
        step.add(output_tokens=STEP.count)
        step.end()
        return {"caught": None}

    @app.get("/step/fail-then-end")
    async def step_fail_then_end() -> _Answer:
        step = steps()
        step.fail(STEP.failure_reason)
        step.end()
        return {"caught": None}

    @app.get("/step/refuse-then-end")
    async def step_refuse_then_end() -> _Answer:
        step = steps()
        step.refuse(STEP_REFUSAL_REASON)
        step.end()
        return {"caught": None}

    @app.get("/step/end-cancelled")
    async def step_end_cancelled() -> _Answer:
        step = steps()
        step.add(output_tokens=STEP.count)
        step.end_cancelled()
        return {"caught": None}

    @app.get("/step/sync-end-cancelled")
    def step_sync_end_cancelled() -> _Answer:
        steps().end_cancelled()
        return {"caught": None}


def _add_step_end_cancelled_routes(app: FastAPI, steps: Steps) -> None:
    """Routes that end a step as cancelled after another outcome."""

    @app.get("/step/fail-then-end-cancelled")
    async def step_fail_then_end_cancelled() -> _Answer:
        step = steps()
        step.fail(STEP.failure_reason)
        step.end_cancelled()
        return {"caught": None}

    @app.get("/step/refuse-then-end-cancelled")
    async def step_refuse_then_end_cancelled() -> _Answer:
        step = steps()
        step.refuse(STEP_REFUSAL_REASON)
        step.end_cancelled()
        return {"caught": None}

    @app.get("/step/handled-then-end-cancelled")
    async def step_handled_then_end_cancelled() -> _Answer:
        step = steps()
        step.handled()
        step.end_cancelled()
        return {"caught": None}

    @app.get("/step/degraded-then-end-cancelled")
    async def step_degraded_then_end_cancelled() -> _Answer:
        step = steps()
        step.degraded()
        step.end_cancelled()
        return {"caught": None}

    @app.get("/step/end-cancelled-then-raise")
    async def step_end_cancelled_then_raise() -> _Answer:
        failure = ValueError(STEP_FAILURE_MESSAGE)
        try:
            async with steps() as step:
                step.end_cancelled()
                raise failure
        except ValueError as caught:
            return _caught(caught, failure)


def _add_step_end_order_routes(app: FastAPI, steps: Steps) -> None:
    """Routes that end a step more than once or inside its block."""

    @app.get("/step/end-cancelled-then-end")
    async def step_end_cancelled_then_end() -> _Answer:
        step = steps()
        step.end_cancelled()
        step.end()
        return {"caught": None}

    @app.get("/step/end-then-end-cancelled")
    async def step_end_then_end_cancelled() -> _Answer:
        step = steps()
        step.end()
        step.end_cancelled()
        return {"caught": None}

    @app.get("/step/end-cancelled-twice")
    async def step_end_cancelled_twice() -> _Answer:
        step = steps()
        step.end_cancelled()
        step.end_cancelled()
        return {"caught": None}

    @app.get("/step/end-cancelled-in-block")
    async def step_end_cancelled_in_block() -> _Answer:
        async with steps() as step:
            step.end_cancelled()
        return {"caught": None}


def _add_step_start_routes(app: FastAPI, steps: Steps) -> None:
    """Routes that build a step apart from its block, start it earlier or end it synchronously."""

    @app.get("/step/sync-end")
    def step_sync_end() -> _Answer:
        steps().end()
        return {"caught": None}

    @app.get("/step/constructed-before-the-block")
    async def step_constructed_before_the_block() -> _Answer:
        step = steps()
        steps.advance(STEP.seconds_before_first_output)
        step.first_output()
        steps.advance(STEP.seconds_after_first_output)
        async with step:
            pass
        return {"caught": None}

    @app.get("/step/started-earlier")
    async def step_started_earlier() -> _Answer:
        step = steps(started_at=steps.now() - STEP.earlier_start_seconds)
        step.first_output()
        steps.advance(STEP.seconds_after_first_output)
        step.end()
        return {"caught": None}

    @app.get("/step/started-in-the-future")
    async def step_started_in_the_future() -> _Answer:
        try:
            steps(started_at=steps.now() + STEP.earlier_start_seconds)
        except ValueError as caught:
            return {"caught": str(caught)}
        return {"caught": None}

    @app.get("/step/end-twice")
    async def step_end_twice() -> _Answer:
        step = steps()
        step.end()
        step.end()
        return {"caught": None}

    @app.get("/step/end-in-block")
    async def step_end_in_block() -> _Answer:
        async with steps() as step:
            step.end()
        return {"caught": None}

    @app.get("/step/end-then-raise")
    async def step_end_then_raise() -> _Answer:
        failure = ValueError(STEP_FAILURE_MESSAGE)
        try:
            async with steps() as step:
                step.end()
                raise failure
        except ValueError as caught:
            return _caught(caught, failure)


def _add_step_thread_routes(app: FastAPI, steps: Steps) -> None:
    """Routes that run a step on a thread."""

    @app.get("/step/thread")
    async def step_thread() -> _Answer:
        await asyncio.to_thread(_sync_step, steps, failure=None)
        return {"caught": None}

    @app.get("/step/thread-failed")
    async def step_thread_failed() -> _Answer:
        failure = ValueError(STEP_FAILURE_MESSAGE)
        try:
            await asyncio.to_thread(_sync_step, steps, failure=failure)
        except ValueError as caught:
            return _caught(caught, failure)
        return {"caught": None}

    @app.get("/step/sync-endpoint")
    def step_sync_endpoint() -> _Answer:
        _sync_step(steps, failure=None)
        return {"caught": None}


def add_step_routes(app: FastAPI) -> None:
    """Add every step route, on one clock the routes advance."""
    steps = Steps()
    _add_step_outcome_routes(app, steps)
    _add_step_reason_routes(app, steps)
    _add_step_lifetime_routes(app, steps)
    _add_step_field_routes(app, steps)
    _add_step_failure_routes(app, steps)
    _add_step_conflict_routes(app, steps)
    _add_step_end_routes(app, steps)
    _add_step_end_cancelled_routes(app, steps)
    _add_step_end_order_routes(app, steps)
    _add_step_start_routes(app, steps)
    _add_step_thread_routes(app, steps)
