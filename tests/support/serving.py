import asyncio
import socket
from collections.abc import Awaitable, Callable

import uvicorn


def serve(config: uvicorn.Config, exercise: Callable[[str], Awaitable[None]]) -> None:
    """Serve `config` with a real uvicorn server while `exercise` runs, then stop it.

    `exercise` gets the server's base URL. The listening socket exists before the server runs,
    so a request made at once waits in its backlog and nothing needs polling.
    """

    async def run() -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(config)
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            await exercise(base_url)
        finally:
            server.should_exit = True
            await serving
            listener.close()

    asyncio.run(run())
