"""A host run with `-X thread_inherit_context=1` whose pool worker starts inside a trace.

Its worker keeps the context of the thread that started it. The host then sends one request from
a work item submitted outside a trace and one from an item submitted inside another, as
`<url> <the trace the worker starts in> <the trace of the second item>` say.
"""

import http.client
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from logline import configure, unit_trace


def send(url: str) -> None:
    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port)
    connection.request("GET", "/")
    connection.getresponse().read()
    connection.close()


def main(url: str, starting_traceparent: str, later_traceparent: str) -> None:
    configure("inherited-context-host")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with unit_trace(starting_traceparent):
            pool.submit(int).result()
        pool.submit(send, url).result()
        with unit_trace(later_traceparent):
            pool.submit(send, url).result()


if __name__ == "__main__":
    main(*sys.argv[1:])
