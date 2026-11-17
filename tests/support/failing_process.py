"""A host process that configures logline, serves briefly and then fails as `argv[1]` names."""

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


def _raise_in_a_thread() -> None:
    def fail() -> None:
        raise RuntimeError(SECRET)

    thread = threading.Thread(target=fail)
    thread.start()
    thread.join()


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
