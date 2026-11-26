"""A host process that configures logline, serves briefly and then fails as `argv[1]` names."""

import asyncio
import atexit
import gc
import sys
import threading
import warnings

import uvicorn
from starlette.types import Receive, Scope, Send

from logline import configure

SECRET = "secret-message-text"


def _serve_until_started() -> None:
    """Run a real uvicorn server that asks itself to stop once its lifespan has started."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send({"type": "lifespan.startup.complete"})
        server.should_exit = True
        await receive()
        await send({"type": "lifespan.shutdown.complete"})

    server = uvicorn.Server(uvicorn.Config(app, port=0, log_config=None, lifespan="on"))
    server.run()


def raise_the_failure() -> None:
    raise RuntimeError(SECRET)


async def fail_a_task() -> None:
    raise_the_failure()


def _raise_in_a_thread() -> None:
    thread = threading.Thread(target=raise_the_failure)
    thread.start()
    thread.join()


class Leaky:
    """An object whose cleanup raises, which Python reports as an unraisable exception."""

    def __del__(self) -> None:
        raise_the_failure()


async def forget_a_failing_task() -> None:
    asyncio.get_running_loop().create_task(fail_a_task())
    await asyncio.sleep(0)
    gc.collect()


if __name__ == "__main__":
    configure("failing")
    _serve_until_started()
    match sys.argv[1]:
        case "crash":
            raise RuntimeError(SECRET)
        case "thread":
            _raise_in_a_thread()
        case "warning":
            warnings.warn(SECRET, DeprecationWarning, stacklevel=1)
        case "task":
            asyncio.run(forget_a_failing_task())
        case "unraisable":
            Leaky()
        case "unraisable_in_exit_handler":
            atexit.register(Leaky)
        case "unraisable_at_teardown":
            # Python cleans a module global up only while the interpreter shuts down.
            _HELD_UNTIL_TEARDOWN = Leaky()
