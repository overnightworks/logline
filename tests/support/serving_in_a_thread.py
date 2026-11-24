import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer


@contextmanager
def serving_in_a_thread(server: ThreadingHTTPServer) -> Iterator[None]:
    """Serve with `serve_forever` in a thread; leaving stops it and closes the server.

    `server_close` runs last, so a server whose threads are not daemons has joined them by then.
    """
    serving = threading.Thread(target=server.serve_forever)
    serving.start()
    try:
        yield
    finally:
        server.shutdown()
        serving.join()
        server.server_close()
