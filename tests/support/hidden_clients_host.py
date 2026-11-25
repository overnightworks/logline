"""A host that has neither httpx nor httpx2: it configures twice and calls through `http.client`.

It prints the headers `outgoing_headers()` gave inside the trace, as one JSON object, and nothing
else: any line `configure` wrote would show up as more output.
"""

import http.client
import json
import sys
from urllib.parse import urlsplit


def main(url: str) -> None:
    # A `None` entry makes `import httpx` raise ImportError and `find_spec` report no package.
    sys.modules["httpx"] = None  # pyright: ignore[reportArgumentType]
    sys.modules["httpx2"] = None  # pyright: ignore[reportArgumentType]
    from logline import configure, outgoing_headers  # noqa: PLC0415
    from logline.trace import request_trace  # noqa: PLC0415

    configure("hidden-clients-host")
    configure("hidden-clients-host")
    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port)
    with request_trace([]):
        connection.request("GET", "/")
        connection.getresponse().read()
        sys.stdout.write(json.dumps(outgoing_headers()))
    connection.close()


if __name__ == "__main__":
    main(sys.argv[1])
