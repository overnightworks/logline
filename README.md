# logline

The fleet's shared Python logger. Every app and service that calls it writes the same JSON record to
stdout, one request line per HTTP request, and carries W3C trace context across calls. It builds on
[structlog](https://www.structlog.org/) and [opentelemetry-api](https://opentelemetry.io/docs/languages/python/);
it imports no web framework. The reader is the developer who wires a host in.

Distribution `overnightworks-logline`, import package `logline`.

## Install

`overnightworks-logline` is not on PyPI. A host pins a tag by git. Put the tag in place of `vX.Y.Z`:

```toml
dependencies = [
    "overnightworks-logline @ git+https://github.com/overnightworks/logline@vX.Y.Z",
]
```

A host's Dependabot entry for uv ignores `overnightworks-*`, because Dependabot looks the name up on PyPI.
Tags are never moved; a broken release gets the next patch tag.

## Quick start

```python
import uvicorn
from fastapi import FastAPI, Request

from logline import RequestLineMiddleware, configure, outgoing_headers, set_sub

configure("notes")

app = FastAPI()
app.add_middleware(RequestLineMiddleware)


@app.get("/notes/{note_id}")
async def read_note(note_id: int, request: Request) -> dict[str, object]:
    set_sub(request.scope, "the-persons-sub")
    return {"callee_headers": outgoing_headers()}


if __name__ == "__main__":
    uvicorn.run(app, log_config=None)
```

- `configure(service)` runs once at the entry point, before the first line is written.
- `app.add_middleware(RequestLineMiddleware)` goes after the app's other middleware, so they run inside the
  request's trace.
- `set_sub(scope, sub)` and `set_client_ip(scope, ip)` take the ASGI scope. Call them from an async or a sync
  endpoint, from a dependency, or from below a `BaseHTTPMiddleware`. Outside an HTTP request they do nothing.

## The record

Every line, from structlog, from the standard library, from uvicorn and from other libraries, is one JSON
object with these keys, plus the key-value fields the caller adds:

| Key | Value |
|---|---|
| `ts` | ISO 8601, UTC |
| `level` | `debug`, `info`, `warning`, `error` or `critical` |
| `service` | the name given to `configure` |
| `version` | the environment variable `SERVICE_VERSION` at `configure`, `null` when it is unset |
| `logger` | the logger's name |
| `event` | the message |
| `trace_id` | the running request's trace id as 32 hex digits, `null` outside a request |

A caller's own field named `ts`, `level`, `service`, `version`, `logger` or `trace_id` is overwritten. An
exception renders as text in `exception`: its frames and the type of each exception in the chain,
never a message.
An image build bakes `SERVICE_VERSION` from the release it builds.

## The request line

The middleware writes one line per HTTP request on the logger `logline.request`, with `event` `request` and
`method`, `route`, `status`, `duration_ms`, `sub` and `ip`:

- `route` is the route template (`/notes/{note_id}`), never the path. A request that matched no route logs
  `route: null` and status 404. An exception that reaches the middleware logs 500, or the status already sent.
- `sub` is `null` until the host calls `set_sub`.
- `ip` is the ASGI client address until the host calls `set_client_ip`.
- `duration_ms` runs until the last body chunk is sent.
- WebSocket connections get no line.

The line is written at INFO whatever `LOG_LEVEL` says: no setting turns it off.

### The client address

A host with its own client-address decider hands its answer over, for example webauth's:

```python
set_client_ip(request.scope, resolve_client_ip(request))
```

A host without one runs uvicorn with proxy headers, trusting only the network of its reverse proxy:

```python
uvicorn.run(app, log_config=None, proxy_headers=True, forwarded_allow_ips="<the proxy's network>")
```

## Trace context

An incoming `traceparent` continues that trace; `tracestate` and the flags pass through. Without a valid one
the request starts a new trace with flags `00`. Each request gets a fresh span id. The trace lives in
OpenTelemetry's context, so `trace_id` reaches lines written from sync endpoints on worker threads too.
There is no baggage, no SDK and no exporter.

`outgoing_headers()` returns the current context as the headers of an outgoing call, an empty dict outside a
request: `httpx.get(url, headers=outgoing_headers())`.

## Settings

| Variable | Effect |
|---|---|
| `LOG_LEVEL` | the root level: `debug`, `info`, `warning`, `error` or `critical`, case-insensitive; `info` when unset. Anything else, an empty value included, makes `configure` raise `ValueError` |
| `SERVICE_VERSION` | the `version` of every line |

## Starting uvicorn

Call `configure` first, then give uvicorn `log_config=None` and no `log_level`: `uvicorn.run(app, log_config=None)`
or `uvicorn.Config(app, log_config=None)`. Under the uvicorn command line, call `configure` where the app
module is imported or in the app factory. `configure` routes uvicorn's loggers to the JSON handler and keeps
uvicorn's access log silent in every start mode, because the request line replaces it.

## Never logged

logline writes no credentials, tokens, cookies, header values, contents (bodies, messages, prompts,
transcripts, audio), raw paths or query strings, and a host adds none through a field: it logs ids and the
`trace_id`. For this, `configure` also:

- renders exceptions as text, never as a dict with local variables and never with a message, which a
  library fills with the URL it called, its query and the redirect location; a host logs the ids it needs
  as fields;
- sets the `httpx`, `httpx2` and `httpcore` loggers to WARNING, because their INFO lines carry the full URL
  with its query; the callee's request line with the same `trace_id` covers the call;
- drops uvicorn's WebSocket connection lines, which carry the raw path with its query.

## Versioning

The version follows [semantic versioning](https://semver.org/). While it is 0.x, a minor release may break a
public contract and says so under **Breaking** in the
[release pages](https://github.com/overnightworks/logline/releases); a patch release never does. A module's
public names are the names its `__all__` lists; everything else may change in any release.

## Development

[AGENTS.md](AGENTS.md) holds the repository's commands and rules. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
says which module owns what; [docs/VISION.md](docs/VISION.md) says what logline is for.

## Licence

MIT, see [LICENSE](LICENSE).
