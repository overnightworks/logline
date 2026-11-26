import asyncio
import gc
import logging
from collections.abc import Callable, Coroutine

import httpx
import pytest
import uvicorn
from starlette.types import Receive, Scope, Send

from logline import RequestLineMiddleware
from logline.standard import BASE_KEYS
from logline.testing import configured
from tests.support.failing_process import (
    SECRET,
    fail_a_task,
    forget_a_failing_task,
    raise_the_failure,
)
from tests.support.incoming_trace import INCOMING_TRACE_ID, INCOMING_TRACEPARENT
from tests.support.lines import Line, json_lines, lines_of
from tests.support.outgoing_clients import inside_a_request
from tests.support.serving import serve

_ASYNCIO_LOGGER = "asyncio"
_ARGUMENT_TEXT = "callback-argument-text"
_OTHER_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
_OTHER_TRACEPARENT = f"00-{_OTHER_TRACE_ID}-b7ad6b7169203331-01"
_FAILURE_KEYS = BASE_KEYS | {"error_type", "exception"}
_TASK_FAILED = "asyncio_task.failed"
_CALLBACK_FAILED = "asyncio_callback.failed"
_UNKNOWN_RECORD = "asyncio.unknown_record"

type Context = dict[str, object]


async def _forget_a_failed_future() -> None:
    future = asyncio.get_running_loop().create_future()
    future.set_exception(RuntimeError(SECRET))
    del future
    gc.collect()


async def _cancel_a_shield_whose_inner_task_then_fails() -> None:
    failing = asyncio.Event()

    async def fail_when_told() -> None:
        await failing.wait()
        raise RuntimeError(SECRET)

    shielded = asyncio.shield(asyncio.ensure_future(fail_when_told()))
    await asyncio.sleep(0)
    shielded.cancel()
    failing.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def _leave_a_task_that_fails_while_asyncio_run_shuts_down() -> None:
    async def fail_when_cancelled() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise RuntimeError(SECRET) from None

    asyncio.get_running_loop().create_task(fail_when_cancelled())
    await asyncio.sleep(0)


async def _fail_a_client_connected_callback() -> None:
    async def callback(_reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
        raise RuntimeError(SECRET)

    server = await asyncio.start_server(callback, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await reader.read()
    writer.close()
    server.close()
    await server.wait_closed()


async def _fail_a_callback() -> None:
    def callback(_argument: str) -> None:
        raise_the_failure()

    asyncio.get_running_loop().call_soon(callback, _ARGUMENT_TEXT)
    await asyncio.sleep(0)


def _report(
    make_context: Callable[[asyncio.AbstractEventLoop], Context],
) -> Callable[[], Coroutine[object, object, None]]:
    """Hand asyncio's handler the context a library builds, as asyncio's own code does."""

    async def report() -> None:
        loop = asyncio.get_running_loop()
        loop.call_exception_handler(make_context(loop))

    return report


def _task_group_context(loop: asyncio.AbstractEventLoop) -> Context:
    task = loop.create_task(asyncio.sleep(0))
    return {
        "message": f"Task {task!r} has errored out but its parent task is completed",
        "exception": RuntimeError(SECRET),
        "task": task,
    }


def _transport_context(_loop: asyncio.AbstractEventLoop) -> Context:
    return {
        "message": "Fatal error on transport",
        "exception": RuntimeError(SECRET),
        "transport": object(),
    }


def _future_with_another_message_context(loop: asyncio.AbstractEventLoop) -> Context:
    return {
        "message": "some library's own message",
        "exception": RuntimeError(SECRET),
        "future": loop.create_future(),
    }


async def _forget_a_stuck_task() -> None:
    loop = asyncio.get_running_loop()
    _start_a_task_waiting_for_a_future(loop)
    await asyncio.sleep(0)
    gc.collect()


def _start_a_task_waiting_for_a_future(loop: asyncio.AbstractEventLoop) -> None:
    future = loop.create_future()

    async def wait_for_it() -> None:
        await future

    loop.create_task(wait_for_it())


def _the_asyncio_lines(output: str) -> list[Line]:
    return lines_of(json_lines(output), _ASYNCIO_LOGGER)


def _without_traceback(line: Line) -> str:
    return str({key: value for key, value in line.items() if key != "exception"})


@pytest.mark.parametrize(
    ("main", "event"),
    [
        pytest.param(forget_a_failing_task, _TASK_FAILED, id="task exception nobody retrieved"),
        pytest.param(_forget_a_failed_future, _TASK_FAILED, id="future exception nobody retrieved"),
        pytest.param(
            _cancel_a_shield_whose_inner_task_then_fails, _TASK_FAILED, id="shielded future fails"
        ),
        pytest.param(
            _leave_a_task_that_fails_while_asyncio_run_shuts_down,
            _TASK_FAILED,
            id="task fails during asyncio.run shutdown",
        ),
        pytest.param(
            _fail_a_client_connected_callback, _TASK_FAILED, id="client_connected_cb raises"
        ),
        pytest.param(_report(_task_group_context), _TASK_FAILED, id="task group task errored out"),
        pytest.param(_fail_a_callback, _CALLBACK_FAILED, id="callback raises"),
        pytest.param(_report(_transport_context), _CALLBACK_FAILED, id="fatal transport error"),
        pytest.param(
            _report(_future_with_another_message_context),
            _CALLBACK_FAILED,
            id="future key under a message of the library's own",
        ),
    ],
)
def test_a_failure_asyncio_reports_writes_one_coded_error_line_without_its_text(
    capsys: pytest.CaptureFixture[str],
    main: Callable[[], Coroutine[object, object, None]],
    event: str,
) -> None:
    with configured("asyncio-failures"):
        asyncio.run(main())

    captured = capsys.readouterr()
    (line,) = _the_asyncio_lines(captured.out)
    assert (line["event"], line["level"], line["error_type"]) == (event, "error", "RuntimeError")
    assert line.keys() == _FAILURE_KEYS
    assert SECRET in str(line["exception"])
    assert SECRET not in _without_traceback(line)
    assert (line["trace_id"], captured.err) == (None, "")


def test_a_failed_callback_line_carries_none_of_the_callbacks_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with configured("asyncio-callback"):
        asyncio.run(_fail_a_callback())

    assert _ARGUMENT_TEXT not in capsys.readouterr().out


def test_a_task_destroyed_while_pending_writes_an_unknown_record_without_exception(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with configured("asyncio-destroyed"):
        asyncio.run(_forget_a_stuck_task())

    (line,) = _the_asyncio_lines(capsys.readouterr().out)
    assert (line["event"], line["level"]) == (_UNKNOWN_RECORD, "error")
    assert line.keys() == BASE_KEYS


@pytest.mark.parametrize("level", [logging.INFO, logging.WARNING, logging.ERROR])
def test_a_record_asyncio_writes_without_an_exception_is_named_at_its_own_level(
    capsys: pytest.CaptureFixture[str], level: int
) -> None:
    with configured("asyncio-unknown"):
        logging.getLogger(_ASYNCIO_LOGGER).log(
            level, "Executing %s took %.3f seconds", _ARGUMENT_TEXT, 1
        )

    (line,) = _the_asyncio_lines(capsys.readouterr().out)
    assert (line["event"], line["level"]) == (_UNKNOWN_RECORD, logging.getLevelName(level).lower())
    assert _ARGUMENT_TEXT not in str(line)


def test_a_record_asyncio_writes_with_an_exception_outside_its_handler_is_a_callback_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with configured("asyncio-direct"), inside_a_request():
        try:
            raise RuntimeError(SECRET)
        except RuntimeError:
            logging.getLogger(_ASYNCIO_LOGGER).warning("Exception in a loop", exc_info=True)

    (line,) = _the_asyncio_lines(capsys.readouterr().out)
    assert (line["event"], line["level"], line["error_type"]) == (
        _CALLBACK_FAILED,
        "error",
        "RuntimeError",
    )
    assert (line.keys(), line["trace_id"]) == (_FAILURE_KEYS, None)
    assert "Exception in a loop" not in _without_traceback(line)


def test_a_record_below_info_passes_as_asyncio_wrote_it(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")

    with configured("asyncio-debug"):
        logging.getLogger(_ASYNCIO_LOGGER).debug("Using %s", "a selector")

    (line,) = _the_asyncio_lines(capsys.readouterr().out)
    assert (line["event"], line["level"]) == ("Using a selector", "debug")


def test_a_host_exception_handler_wins_over_the_coded_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    handled: list[BaseException] = []

    async def main() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: handled.append(context["exception"]))
        await forget_a_failing_task()

    with configured("asyncio-host-handler"):
        asyncio.run(main())

    assert (len(handled), capsys.readouterr().out) == (1, "")


def test_a_host_handler_that_falls_back_writes_the_coded_line_with_the_tasks_trace(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def main() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda loop, context: loop.default_exception_handler(context))
        with inside_a_request():
            loop.create_task(fail_a_task())
        await asyncio.sleep(0)
        gc.collect()

    with configured("asyncio-fallback"):
        asyncio.run(main())

    (line,) = _the_asyncio_lines(capsys.readouterr().out)
    assert (line["event"], line["trace_id"]) == (_TASK_FAILED, INCOMING_TRACE_ID)


def test_a_failed_task_reported_by_itself_inside_its_own_context_carries_its_trace(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def report_from_inside_the_task() -> None:
        task = asyncio.current_task()
        asyncio.get_running_loop().call_exception_handler(
            {"message": "reported", "exception": RuntimeError(SECRET), "task": task}
        )

    async def main() -> None:
        with inside_a_request():
            await asyncio.get_running_loop().create_task(report_from_inside_the_task())

    with configured("asyncio-entered"):
        asyncio.run(main())

    (line,) = _the_asyncio_lines(capsys.readouterr().out)
    assert line["trace_id"] == INCOMING_TRACE_ID


class _LookupFails(dict[str, object]):
    """A context whose `task` lookup raises, which asyncio's own handler never reads."""

    def get(self, key: str, default: object = None) -> object:
        if key == "task":
            raise LookupError(key)
        return super().get(key, default)


def test_a_context_logline_cannot_read_still_writes_one_coded_line_and_raises_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def main() -> None:
        loop = asyncio.get_running_loop()
        loop.default_exception_handler(
            _LookupFails({"message": "unreadable", "exception": RuntimeError(SECRET)})
        )

    with configured("asyncio-unreadable"):
        asyncio.run(main())

    (line,) = _the_asyncio_lines(capsys.readouterr().out)
    assert (line["event"], line["error_type"]) == (_CALLBACK_FAILED, "RuntimeError")


def test_a_context_whose_exception_is_not_an_exception_writes_one_unknown_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with configured("asyncio-not-an-exception"):
        asyncio.run(_report(lambda _loop: {"message": "m", "exception": "not an exception"})())

    captured = capsys.readouterr()
    (line,) = _the_asyncio_lines(captured.out)
    assert (line["event"], line.keys(), captured.err) == (_UNKNOWN_RECORD, BASE_KEYS, "")


def test_leaving_the_block_gives_asyncio_its_own_records_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with configured("asyncio-restored"):
        pass

    with caplog.at_level(logging.ERROR, logger=_ASYNCIO_LOGGER):
        asyncio.run(forget_a_failing_task())

    (record,) = [record for record in caplog.records if record.name == _ASYNCIO_LOGGER]
    assert record.getMessage().startswith("Task exception was never retrieved")


class _FailuresLeftBehind:
    """An ASGI app whose requests leave behind failures that nobody retrieves."""

    def __init__(self) -> None:
        self._kept: list[object] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            raise RuntimeError("no lifespan")
        loop = asyncio.get_running_loop()
        match scope["path"]:
            case "/task":
                self._kept.append(loop.create_task(fail_a_task()))
            case "/future":
                future = loop.create_future()
                future.set_exception(RuntimeError(SECRET))
                self._kept.append(future)
            case "/callback":
                loop.call_soon(raise_the_failure)
            case "/collect":
                self._kept.clear()
                gc.collect()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})


def _lines_after_requests(
    capsys: pytest.CaptureFixture[str], requests: list[tuple[str, str]]
) -> list[Line]:
    """Serve the app behind the middleware, make each request with its `traceparent` in turn."""

    async def exercise(base_url: str) -> None:
        async with httpx.AsyncClient(base_url=base_url) as client:
            for path, traceparent in requests:
                await client.get(path, headers={"traceparent": traceparent})

    with configured("asyncio-trace"):
        config = uvicorn.Config(
            RequestLineMiddleware(_FailuresLeftBehind()), log_config=None, lifespan="off"
        )
        serve(config, exercise)
    return _the_asyncio_lines(capsys.readouterr().out)


@pytest.mark.parametrize(
    ("path", "event", "trace_id"),
    [
        pytest.param("/task", _TASK_FAILED, INCOMING_TRACE_ID, id="task"),
        pytest.param("/callback", _CALLBACK_FAILED, INCOMING_TRACE_ID, id="callback"),
        pytest.param("/future", _TASK_FAILED, None, id="plain future"),
    ],
)
def test_a_failure_carries_the_trace_of_the_request_that_started_it_not_the_current_one(
    capsys: pytest.CaptureFixture[str], path: str, event: str, trace_id: str | None
) -> None:
    lines = _lines_after_requests(
        capsys, [(path, INCOMING_TRACEPARENT), ("/collect", _OTHER_TRACEPARENT)]
    )

    (line,) = lines
    assert (line["event"], line["trace_id"]) == (event, trace_id)
