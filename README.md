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
  They may run at any point before the response ends, so a sign-in callback that establishes the person calls
  `set_sub` itself.
- `set_route(scope, template)` names the route of an answer the framework leaves without one; see
  [The request line](#the-request-line).

## The record

Every line, from structlog, from the standard library, from uvicorn and from other libraries, is one JSON
object with these keys, plus the key-value fields the caller adds:

| Key | Value |
|---|---|
| `ts` | ISO 8601, UTC |
| `level` | `debug`, `info`, `warning`, `error` or `critical` |
| `service` | the name given to `configure` |
| `version` | the environment variable `SERVICE_VERSION` at `configure`, `null` when it is unset or empty |
| `logger` | the logger's name |
| `event` | the message |
| `trace_id` | the running request's trace id as 32 hex digits, `null` outside a request |

A caller's own field named `ts`, `level`, `service`, `version`, `logger` or `trace_id` is overwritten. An
exception renders as Python's text traceback in `exception`: its frames, its message and the whole chain
(`__cause__`, `__context__`, the members of an `ExceptionGroup`), never local variables.
An image build bakes `SERVICE_VERSION` from the release it builds.

## The request line

The middleware writes one line per HTTP request on the logger `logline.request`, with `event`
`logline.REQUEST_EVENT` (`request`) and `method`, `route`, `status`, `duration_ms`, `sub` and `ip`:

- `route` is the route template (`/notes/{note_id}`), never the path: the template the host gave to
  `set_route`, else what the framework left in `scope["route"]`, which inside a mounted FastAPI app is that
  app's own template, relative to the mount. `null` means neither: no route matched (404), a gate answered
  before the router (401), or a `Mount` (static files, a mounted ASGI app) or a plain Starlette `Route`
  answered under FastAPI, which leaves the template only for an `APIRoute`, and the host did not call
  `set_route`. An exception that reaches the middleware logs 500, or the status already sent.
- `sub` is `null` until the host calls `set_sub`.
- `ip` is the ASGI client address until the host calls `set_client_ip`.
- `duration_ms` runs until the last body chunk is sent, and the line is written then; background work after
  the response is not counted, and a `sub` or `ip` set there is too late.
- WebSocket connections get no line.

The line is written at INFO whatever `LOG_LEVEL` says: no setting turns it off.

### The route of a mount or a plain route

Under FastAPI a `Mount` and a plain Starlette `Route` leave no template, so the host names it where it
answers. A later call replaces an earlier one, and `set_route` on an `APIRoute` replaces its template. The
template is a literal pattern such as `/static` or `/mcp`, written in the code, never the raw path or a value
from the request: a path or a request value in `route` puts personal data and unbounded values into every
line. An empty template logs `null`, as when no template is known, and does not fall back to the framework's
route.

```python
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from logline import set_route


def named(template: str, app: ASGIApp) -> ASGIApp:
    async def named_app(scope: Scope, receive: Receive, send: Send) -> None:
        set_route(scope, template)
        await app(scope, receive, send)

    return named_app


async def mcp(request: Request) -> JSONResponse:
    set_route(request.scope, "/mcp")
    return JSONResponse({"ok": True})


app.mount("/static", named("/static", StaticFiles(directory="static")))  # a mounted app
app.router.routes.append(Route("/mcp", mcp))  # a plain Route endpoint
```

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
| `SERVICE_VERSION` | the `version` of every line; unset or empty makes it `null` |

## The host's duties

logline shows what the app logs; the app keeps its logs meaningful and safe:

- An exception message is logged as written. A secret in a message, or in a field the app logs, is the app's
  defect: it raises and logs ids, never tokens or contents.
- No `logging.config.dictConfig` after `configure`: it replaces the handlers `configure` installed.
- uvicorn runs with `log_config=None` (see below).

quality's check rule for hosts is where these duties get checked.

## Starting uvicorn

Call `configure` first, then give uvicorn `log_config=None` and no `log_level`: `uvicorn.run(app, log_config=None)`
or `uvicorn.Config(app, log_config=None)`. Under the uvicorn command line, call `configure` where the app
module is imported or in the app factory. `configure` routes uvicorn's loggers to the JSON handler and keeps
uvicorn's access log silent in every start mode, because the request line replaces it.

## Never logged

At the default level and above, no line carries credentials, tokens, cookies, header values, contents (bodies,
messages, prompts, transcripts, audio) or raw paths and query strings: logline writes none of them, and a host
adds none through a field, so it logs ids and the `trace_id`. At `LOG_LEVEL=debug`, DEBUG records of other
libraries pass as those libraries write them, and some of those write paths, query strings, header values or
frames; logline neither masks nor caps them. `configure` also does the following at every `LOG_LEVEL`:

- renders exceptions as text, never as a dict with local variables;
- sets the `httpx`, `httpx2` and `httpcore` loggers to WARNING, because their INFO lines carry the full URL
  with its query; the callee's request line with the same `trace_id` covers the call;
- drops uvicorn's WebSocket connection lines, which carry the raw path with its query;
- drops records below INFO on `uvicorn.error`, because websockets writes the request path, every
  header and every frame there at DEBUG.

## Testing a host

`logline.testing.configured(service)` is a context manager for a host's in-process tests. It calls
`configure(service)` and on exit restores the process's logging as it was before: the root logger's handlers
and level, the handlers, filters, levels and propagation of the uvicorn, HTTP client and `logline.request`
loggers, structlog's configuration and the stdlib logger class its factory installs. The module imports no
test framework.

```python
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from logline import REQUEST_EVENT, RequestLineMiddleware
from logline.testing import configured


def test_the_app_logs_its_request(capsys):
    app = FastAPI()
    app.add_middleware(RequestLineMiddleware)

    @app.get("/notes/{note_id}")
    def read_note(note_id: str):
        return {"id": note_id}

    with configured("notes"):
        TestClient(app).get("/notes/7")

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    (request,) = [line for line in lines if line["event"] == REQUEST_EVENT]
    assert request["route"] == "/notes/{note_id}"
```

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
