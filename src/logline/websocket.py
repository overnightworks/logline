"""How one WebSocket session ended, read from its ASGI messages under any server.

This module imports no web framework: it speaks ASGI.
"""

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass

from logline.asgi import ASGIApp, Message, Receive, Scope, Send
from logline.step import Outcome

__all__: list[str] = []

_STATUS_OF_CLOSE_BEFORE_ACCEPT = 403
_STATUS_OF_NO_ANSWER = 500
_DEFAULT_CLOSE_CODE_SENT = 1000
_DEFAULT_CLOSE_CODE_RECEIVED = 1005

# Set when the app of a session is entered and never reset: the server writes its own answer to an
# upgrade after the app has returned or raised, in the same task, and the filter on its records
# reads this to tell that the session line already says what happened. A task's context ends
# with the task.
_SESSION_OWNED: ContextVar[bool] = ContextVar("websocket_session_owned", default=False)


def in_owned_session() -> bool:
    """Whether the current task runs an app whose session line the middleware writes."""
    return _SESSION_OWNED.get()


@dataclass(frozen=True, slots=True)
class SessionEnd:
    """What the ASGI messages and the app's exit say about how a session ended."""

    outcome: Outcome
    status: int | None
    close_code: int | None


@dataclass(slots=True)
class _Session:
    accepted: bool = False
    answer_status: int | None = None
    close_code: int | None = None
    raised: Outcome | None = None

    def noting_received(self, message: Message) -> None:
        if message["type"] == "websocket.disconnect":
            self.close_code = message.get("code", _DEFAULT_CLOSE_CODE_RECEIVED)

    def noting_sent(self, message: Message) -> None:
        match message["type"]:
            case "websocket.accept":
                self.accepted = True
            case "websocket.http.response.start":
                self.answer_status = message["status"]
            case "websocket.close" if self.accepted:
                self.close_code = message.get("code", _DEFAULT_CLOSE_CODE_SENT)
            case "websocket.close":
                self.answer_status = _STATUS_OF_CLOSE_BEFORE_ACCEPT

    def end(self) -> SessionEnd:
        if self.accepted:
            return SessionEnd(self.raised or Outcome.OK, None, self.close_code)
        status = self.answer_status or _STATUS_OF_NO_ANSWER
        denied = self.answer_status is not None
        outcome = self.raised or (Outcome.REFUSED if denied else Outcome.FAILED)
        return SessionEnd(outcome, status, None)


async def run_websocket(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    write_line: Callable[[SessionEnd], None],
) -> None:
    """Run the app's `websocket` scope, then hand `write_line` how the session ended.

    An accepted session is `ok` unless the app raised; `close_code` is the code of the last close
    message, from either side. An upgrade the app did not accept ends with the HTTP `status` the
    client got: `refused` when the app answered it (a denial response, or 403 for a close before
    accept), `failed` when it returned without answering, and the app's own exception in either
    case.
    """
    _SESSION_OWNED.set(True)
    session = _Session()

    async def receive_noting_close() -> Message:
        message = await receive()
        session.noting_received(message)
        return message

    async def send_noting_answer(message: Message) -> None:
        session.noting_sent(message)
        await send(message)

    try:
        await app(scope, receive_noting_close, send_noting_answer)
    except Exception:
        session.raised = Outcome.FAILED
        raise
    except BaseException:
        session.raised = Outcome.CANCELLED
        raise
    finally:
        write_line(session.end())
