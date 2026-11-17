"""The app startup and shutdown lines, written from the ASGI `lifespan` scope under any server.

This module imports no web framework: it speaks ASGI.
"""

import sys
from enum import StrEnum

import structlog

from logline.asgi import ASGIApp, Message, Receive, Scope, Send

__all__ = ["LIFESPAN_LOGGER_NAME", "run_lifespan"]

LIFESPAN_LOGGER_NAME = "logline.lifespan"

_logger = structlog.get_logger(LIFESPAN_LOGGER_NAME)


class _Phase(StrEnum):
    STARTUP = "startup"
    SHUTDOWN = "shutdown"


_PHASE_OF_RECEIVED = {
    "lifespan.startup": _Phase.STARTUP,
    "lifespan.shutdown": _Phase.SHUTDOWN,
}
_PHASE_OF_COMPLETED = {
    "lifespan.startup.complete": _Phase.STARTUP,
    "lifespan.shutdown.complete": _Phase.SHUTDOWN,
}
_PHASE_OF_FAILED = {
    "lifespan.startup.failed": _Phase.STARTUP,
    "lifespan.shutdown.failed": _Phase.SHUTDOWN,
}
_COMPLETED_EVENT = {
    _Phase.STARTUP: "app_startup.completed",
    _Phase.SHUTDOWN: "app_shutdown.completed",
}
_FAILED_EVENT = {
    _Phase.STARTUP: "app_startup.failed",
    _Phase.SHUTDOWN: "app_shutdown.failed",
}


def _write_failed(phase: _Phase, failure: BaseException | None) -> None:
    fields: dict[str, object] = {}
    if failure is not None:
        fields = {"exc_info": failure, "error_type": type(failure).__name__}
    _logger.error(_FAILED_EVENT[phase], **fields)


async def run_lifespan(app: ASGIApp, scope: Scope, receive: Receive, send: Send) -> None:
    """Run the app's `lifespan` scope and write one line for each phase it completes or fails.

    A `failed` message carries the traceback of the exception being handled when it is sent, and
    is forwarded without its free-text `message`. An exception raised after the app received a
    phase's message, and before it answered, is that phase's failure; one raised before is the
    ASGI spec's "lifespan unsupported" and writes no line.
    """
    unanswered: _Phase | None = None

    async def receive_noting_phase() -> Message:
        nonlocal unanswered
        message = await receive()
        unanswered = _PHASE_OF_RECEIVED.get(message["type"], unanswered)
        return message

    async def send_answer_writing_line(message: Message) -> None:
        nonlocal unanswered
        if phase := _PHASE_OF_COMPLETED.get(message["type"]):
            _logger.info(_COMPLETED_EVENT[phase])
            unanswered = None
        elif phase := _PHASE_OF_FAILED.get(message["type"]):
            _write_failed(phase, sys.exception())
            message = {key: value for key, value in message.items() if key != "message"}
            unanswered = None
        await send(message)

    try:
        await app(scope, receive_noting_phase, send_answer_writing_line)
    except Exception as failure:
        if unanswered:
            _write_failed(unanswered, failure)
        raise
