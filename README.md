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

A host installed without a lock pins the tag's full commit instead, with the tag in a comment; the standard
"Depending on a fleet package" in overnightworks/quality `docs/release.md` says which hosts and why.

A host's Dependabot entry for uv ignores `overnightworks-*`, because Dependabot looks the name up on PyPI.
Tags are never moved; a broken release gets the next patch tag.

A library on a hop declares `overnightworks-logline` as a version range by name and puts its git URL only in
its dev group; the host pins the git tag, so one logline runs in the process and a tag outside the range is
refused. A library that pins a tag by git URL forces every host to pin the same tag. quality's fleet package
standard owns the rule and its check.

The range is `>=1.0,<2`; [Versioning](#versioning) says what a change to logline means for it.

```toml
# the library
dependencies = ["overnightworks-logline>=1.0,<2"]

[dependency-groups]
dev = ["overnightworks-logline"]

[tool.uv.sources]
overnightworks-logline = { git = "https://github.com/overnightworks/logline", tag = "vX.Y.Z", group = "dev" }
```

The host's `dependencies` stay as above.

## Quick start

```python
import httpx
import uvicorn
from fastapi import FastAPI, Request

from logline import RequestLineMiddleware, configure, set_sub

configure("notes")

app = FastAPI()
app.add_middleware(RequestLineMiddleware)


@app.get("/notes/{note_id}")
async def read_note(note_id: int, request: Request) -> dict[str, object]:
    set_sub(request.scope, "the-persons-sub")
    async with httpx.AsyncClient() as client:
        await client.get(f"http://tags.internal/notes/{note_id}")  # carries the request's trace
    return {"id": note_id}


if __name__ == "__main__":
    uvicorn.run(app, log_config=None)
```

- `configure(service)` runs once at the entry point, before the first line is written. It also makes every
  outgoing HTTP request, and every work item a `ThreadPoolExecutor` runs, carry the current trace, with no
  call-site code ([Outgoing calls](#outgoing-calls)).
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
| `trace_id` | the running trace's id as 32 hex digits: the request's, the WebSocket session's or the turn's; `null` outside a trace |

A line that closes a piece of work also names it: `span_id`, 16 hex digits, is the span the line closes, and
`parent_span_id` the span that started it, `null` for a trace's first span or outside a trace. Those are the lines
the middleware writes for a request and a WebSocket session and every `StepTimer` line; no other line carries
them ([Spans](#spans)). The two names are `closing_line_keys` in `src/logline/standard.json`.

A caller's own field named like one of these keys (`base_keys` in `src/logline/standard.json`) is overwritten. The
scalar rule holds on both write paths: only text, numbers, booleans and `null` reach the line, so an object or a
nested value is dropped from a structlog event as from a standard-library record's `extra=` fields. A caller's field
cannot override these keys or, on the standard-library path, `event`; neither path copies a caller's `exception` (the
key the traceback renders into), and the standard-library path drops uvicorn's `color_message`. A `nan` or infinite
number is written `null`, never `NaN` or `Infinity`. An
exception renders as Python's text traceback in `exception`: its frames, its message and the whole chain
(`__cause__`, `__context__`, the members of an `ExceptionGroup`), never local variables.
An image build bakes `SERVICE_VERSION` from the release it builds.

## The request line

The middleware writes one line per HTTP request on the logger `logline.request`, with `event`
`logline.REQUEST_EVENT` (`request.ended`) and `outcome`, `method`, `route`, `status`, `duration_ms`, `sub`, `ip`,
`span_id` and `parent_span_id`. A host without ASGI writes the same line through `RequestLine` ([below](#a-host-without-asgi)), which the
middleware uses too, so the `method`, `outcome`, `sub`, `ip` and `error_type` rules hold for both. What reads
the ASGI scope, the response messages or the server's answer belongs to the middleware alone, and is marked so; a
`RequestLine` host does that part itself:

- `method` is one of `CONNECT`, `DELETE`, `GET`, `HEAD`, `OPTIONS`, `PATCH`, `POST`, `PUT` and `TRACE`, and any other
  token is `_OTHER`, so the field takes a closed set of values.
- `outcome` is `failed` when the app raised an `Exception` or the status is 5xx, `cancelled` when the app raised
  another `BaseException` (a cancellation), and `ok` otherwise, a 4xx status included: a refused request is
  the app's answer, not a failure. A host compares `event` with `REQUEST_EVENT`, never with the literal.
  Under the middleware such a `BaseException` still reaches the server, which answers 500 before the response
  starts, so the line then reads `cancelled` with `status` 500; once the response has started, the line keeps the
  status already sent, such as 200, with `cancelled`: read the two together.
- `route` is the route template (`/notes/{note_id}`), never the path: the template the host gave to
  `set_route`. Under the middleware, else what the framework left in `scope["route"]`, which inside a mounted
  FastAPI app is that app's own template, relative to the mount. `null` means neither: no route matched (404), a
  gate answered before the router (401), or a `Mount` (static files, a mounted ASGI app) or a plain Starlette
  `Route` or `WebSocketRoute` answered under FastAPI, which leaves the template only for an `APIRoute`, and the host did not call
  `set_route`. An exception that reaches the middleware logs 500, or the status already sent.
- `sub` is `null` until the host calls `set_sub`.
- `ip` is the client address until the host calls `set_client_ip`: under the middleware the ASGI client address,
  for `RequestLine` the `ip` the host passed.
- `duration_ms` runs until the line is written. Under the middleware that is the last body chunk, so background
  work after the response is not counted, and a `sub` or `ip` set there is too late; the line is written then, so
  an exception raised after that leaves the line's `outcome` and `status` as the response wrote them. A
  `RequestLine` ends where the host ends it, `end()` or the end of the block, [below](#a-host-without-asgi).
- The line has no `error_type`. The layer where the exception stops writes the one line that carries it: under
  uvicorn `asgi_app.failed`, for a host without ASGI the host's own failure line.
- `span_id` is the request's span, the one `outgoing_headers()` hands on outside a step, and `parent_span_id` is the
  span id of the incoming `traceparent`, `null` for a request that starts a trace.
- A WebSocket session gets its own line, below.

The line is written at INFO whatever `LOG_LEVEL` says: no setting turns it off.

### The WebSocket session line

The middleware writes one line per WebSocket session when the app returns, on the logger `logline.request` at INFO,
whatever `LOG_LEVEL` says, from the ASGI messages and so under any server. Its `event` is `logline.WEBSOCKET_EVENT` (`websocket.ended`), and a host compares `event` with
`WEBSOCKET_EVENT`, never with the literal. It carries `outcome`, `route`, `duration_ms`, `sub`, `ip`, `close_code`, `span_id` and `parent_span_id`, and `status`
when the upgrade was not accepted. `set_sub`, `set_client_ip` and `set_route` work in a WebSocket scope as they do in a request, and `route`,
`sub` and `ip` read as they do there.

- `outcome` is `ok` for an accepted session, `failed` when the app raised an `Exception` and `cancelled` when it
  raised another `BaseException`. An upgrade the app did not accept is `refused` when the app answered it (a denial
  response, or a close before accept) and `failed` when it returned without answering.
- `status` is the HTTP status the client got: the denial response's, 403 for a close before accept, and 500 when the
  app raised or returned without answering. An accepted session has no `status`.
- `close_code` is the code of the last close message of either side, and `null` when none was seen, such as when the
  app raised and the server closed the socket itself, and for an upgrade that was not accepted.
- `duration_ms` runs from the upgrade to the app's return.
- `span_id` is the session's span, `parent_span_id` the span id of the upgrade's `traceparent`, `null` without a
  valid one.
- `trace_id` is the session's, as for a request: the trace of the upgrade's `traceparent` when it is valid, else a
  new one, so the app's own lines, `asgi_app.failed` and the session line of a refused or failed upgrade read together
  by one id. A turn on the socket has its own trace and the session's is current again after it (see
  [A turn on a WebSocket](#a-turn-on-a-websocket)). Three gaps: one trace spans a whole session, which may last hours,
  and a turn started with `unit_trace(None)` is not linked to it; a call a host makes outside a turn carries the
  session's trace into the service it calls; and uvicorn's legacy `ws="websockets"` implementation writes from
  another task, so its lines stay without a trace.

A host without the middleware gets no session line.

A host that wrapped the whole socket in `unit_trace` drops that wrapper: the session is in a trace already, and a
second one would, without a valid incoming `traceparent`, give the host's lines another id than the session
line. A step that must stay out of the session's trace, such as a call made outside a turn, runs in
`unit_trace(None)`.

**Breaking:** websockets' free-text `connection open` record is dropped, because the session line replaces it, and a
refused upgrade the app answered is the session line with `outcome` `refused` instead of `websocket_upgrade.refused`
(see [Starting uvicorn](#starting-uvicorn)). A host that counts or alerts on the old records reads the new line;
`websocket_handshake.failed` is gone too, and an app that returned without answering is a `failed` session line with
`status` 500.

### The route of a mount or a plain route

Under FastAPI a `Mount`, a plain Starlette `Route` and a `WebSocketRoute` leave no template, so the host names it where it
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

## A host without ASGI

A host whose server is not ASGI, such as one built on the standard library's `http.server`, writes the request line
through `RequestLine(method, headers, *, ip=None)` and never rebuilds it: the `method` coding, the `outcome` rule, the
status when none was sent and the trace are the ones above. `headers` are the request's (name, value) pairs of
text, read case-insensitively, so a host passes its header items as they are; `ip` is the client address, and
`null` in the line when the host has none. The object serves one request. Its stopwatch starts when it is
constructed, and the block runs in the request's trace, the incoming one or a new one, so every line logged inside
carries its `trace_id`. The line carries the request's `span_id` and `parent_span_id` like the middleware's.

```python
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from logline import RequestLine

logger = logging.getLogger(__name__)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with RequestLine(self.command, self.headers.items(), ip=self.client_address[0]) as line:
            line.set_route("/notes")
            line.set_status(200)
            self.send_response(200)
            self.end_headers()


class Server(ThreadingHTTPServer):
    daemon_threads = False

    def handle_error(self, request, client_address):
        logger.exception("request_handling.failed")
```

- `set_sub(sub)`, `set_client_ip(ip)` and `set_route(template)` set the fields at any point of the block, and
  `set_route` takes a literal template, never the path. There is no framework route to fall back on: `route` is
  `null` until the host sets it.
- `set_status(status)` is called where the host sends the status line, not earlier, so the line reports what the
  client got. A request that sets none reads 500.
- The line is written when the block ends. `end()` writes it earlier, for a host whose response ends before the
  block does, and the end of the block then writes nothing more.
- An `Exception` that leaves the block makes the line `failed`, and any other `BaseException` makes it
  `cancelled`, with status 500 in the line when none was set. `RequestLine` sends nothing: what the client gets
  is the host's, and a standard-library server closes the connection without an answer.
- The host's `handle_error` writes the failure line, with the traceback (`logger.exception`) and no request
  value. The exception leaves the block with the trace attached, so that line carries the request's `trace_id`,
  and `request.ended` has no `error_type`.
- A client that disconnects reads as under the middleware: the disconnect exception (`BrokenPipeError`,
  `ConnectionResetError`) leaving the block makes the line `failed`, and one the host catches inside the block
  reads `ok` with the status already set. There is no separate disconnect outcome.
- The standard library's `handle_error` is reached by an `Exception` only. A `BaseException` that is not one ends the
  handler thread, and `configure`'s hook writes `thread.crashed` beside the `cancelled` request line. The
  `thread.crashed` line carries no `trace_id`, so read the two together by time, not by trace id. Time joins them
  only approximately, above all with concurrent requests.
  `SystemExit` is the exception: the request line reads `cancelled` and the hook writes no `thread.crashed`, as for
  any thread that ends with it.
- Known gap: an exception that leaves the block leaves the trace attached to the thread. A host that catches it
  outside the block and keeps the connection stacks the next request's trace on the stale one. The standard
  library closes the connection after `handle_error`, so a host built as above is not affected. A pool worker is not
  affected either: it detaches the context its work item ran in ([Outgoing calls](#outgoing-calls)).

## Trace context

An incoming `traceparent` continues that trace; `tracestate` and the flags pass through. Without a valid one
the request starts a new trace with flags `00`. Each request gets a fresh span id, and its line names it
([Spans](#spans)). The trace lives in
OpenTelemetry's context, so `trace_id` reaches lines written from sync endpoints on worker threads too, and from work
a thread pool runs ([Outgoing calls](#outgoing-calls)).
There is no SDK and no exporter.

The headers logline reads and writes are the ones of OpenTelemetry's global propagator. While `OTEL_PROPAGATORS`
is unset or empty, `configure` sets it to W3C trace context alone: `traceparent` and `tracestate`, and no
`baggage`, continued or forwarded by logline or by any other OpenTelemetry user in the process, FastAPI's own
extraction included. A library that only asks OpenTelemetry to inject headers (`opentelemetry.propagate.inject`)
sends the same trace context as `outgoing_headers()`. A set `OTEL_PROPAGATORS` is the host's deliberate choice:
`configure` leaves the global as OpenTelemetry built it from the variable and `request_trace`, `unit_trace` and
`outgoing_headers()` follow it. With `none` every request and turn starts a new trace, `outgoing_headers()` is
empty and no outgoing request carries a trace header; with `tracecontext,baggage` baggage a handler set is
forwarded, while logline continues no incoming
baggage itself. A host that never calls `configure` keeps OpenTelemetry's default, trace context and baggage.

A host without ASGI gets the same trace from `RequestLine`. `logline.trace.request_trace` takes the pairs a server
hands over as text or as bytes, in any case.

A WebSocket session runs in a trace the same way, from the upgrade's `traceparent` and `tracestate`
([The WebSocket session line](#the-websocket-session-line)).

### Outgoing calls

`configure` hands the current trace to every outgoing HTTP request of the process, a WebSocket opening handshake
through a `websockets` client included, with no call-site code: the
headers of the global propagator, `traceparent` and `tracestate` under the default. Inside a `StepTimer` block that is
the step's span, inside a turn that carries a valid `traceparent` the sender's, and else the request's or the
session's. Outside a trace nothing is added under the default, and `OTEL_PROPAGATORS=none` adds nothing.

Work a process submits to a `concurrent.futures.ThreadPoolExecutor` carries the trace too: each work item runs with
the OpenTelemetry context its submitter had, taken at `submit` and detached when the item ends.
`Executor.map`, asyncio's `loop.run_in_executor` with its default executor and boto3's managed transfers
(`upload_file`, `download_file`, `upload_fileobj`, `download_fileobj`) go through `submit`. An item submitted inside a
trace carries that trace, whichever worker runs it and whatever an earlier item or the worker's start left attached;
one submitted outside a trace carries none, also under `-X thread_inherit_context=1`. Its lines carry the submitter's
`trace_id`, its steps name the submitter's span as `parent_span_id`, and its outgoing requests carry the headers above.
Only OpenTelemetry's context is carried: every other context variable stays as the worker thread has it, and a value
an item sets persists to the next item on that worker, as without logline.

- Covered are every client that goes through the standard library's `http.client` (`urllib.request`, urllib3,
  requests, botocore), httpx and httpx2, `Client` and `AsyncClient`, and the opening handshake of a `websockets`
  client, its asyncio and its sync one, where the host installed them. For httpx and httpx2 that includes custom and
  in-process transports (`ASGITransport`, `MockTransport`). The `websockets` carrier is proven on websockets 15 to 17,
  the range logline's tests run; a host on another range is outside it.
- A request that already carries `traceparent` keeps it, and logline adds nothing to it: a caller's `traceparent` is
  never paired with the current `tracestate`.
- A propagator that raises fails the request, as `outgoing_headers()` does.
- No URL is excluded: third parties receive `traceparent` and `tracestate` too. They carry no user data, but a caller's
  `traceparent` and its passed-through `tracestate` travel on. A redirect to another host keeps the headers.
- Not covered are aiohttp, a CONNECT tunnel, urllib3's optional HTTP/2 and `websockets.legacy`. A call through one of
  them carries the trace only where the host passes `outgoing_headers()` to it.
- boto3's managed transfers through the CRT transfer client (`awscrt` installed, with `preferred_transfer_client` set to
  `"crt"`, or left at `"auto"` on an instance `awscrt` counts as optimized) send through a native client that no hook
  reaches. Choose `preferred_transfer_client="classic"` or set `BOTO_DISABLE_CRT=true`; the classic client is the one
  that carries the trace.
- Not carried into the work, each with the host's remedy:
  - An executor whose class overrides `prepare_context` (`InterpreterPoolExecutor`) and `ProcessPoolExecutor` run
    items in another interpreter or process: pass `current_traceparent()` as an argument and run the item inside
    `unit_trace(traceparent)`.
  - An executor that overrides `submit` without calling it: submit `contextvars.copy_context().run, fn` instead of `fn`.
  - `multiprocessing.pool.ThreadPool` and `multiprocessing.dummy.Pool` run their own threads without
    `concurrent.futures`, and take no `context=`: run each task under `contextvars.copy_context().run`, or use a
    `ThreadPoolExecutor`.
  - A `threading.Thread` or `threading.Timer` a host or library starts: a thread doing one piece of the caller's work is
    started with `context=contextvars.copy_context()`.
  - Under `-X thread_inherit_context=1` a long-lived thread started inside a trace keeps that trace, so its later
    lines and requests carry it. That includes a done callback (`Future.add_done_callback`), which runs on the worker
    after its item ends, and work submitted from inside that callback. Start such a thread outside a trace.

`outgoing_headers()` returns the current context as the headers of an outgoing call, an empty dict outside a request,
a WebSocket session or a turn.

**Breaking:** work submitted to a `ThreadPoolExecutor` carries its submitter's trace: its outgoing requests carry
`traceparent` and `tracestate`, third parties included, its lines carry the submitter's `trace_id` and its steps name
the submitter's span as `parent_span_id`. Migration: none required; a host whose tests assert the headers or trace ids
of work sent from a thread pool sees the submitter's trace. A hand-written `contextvars.copy_context().run` around such
work, there only for the trace, may go.

**Breaking:** an outgoing request through a covered client carries `traceparent` and `tracestate` where the host did
not pass them, in-process test transports included. A host's tests through `ASGITransport` continue the caller's
trace, and a test that asserts a request's exact headers (respx, `MockTransport`) sees the new ones. A hand-written
carrier of the host, such as a wrapper that adds `outgoing_headers()` to each call, is no longer needed and
may go; no host has to change. If a carrier breaks a client, pin the previous logline tag.

### Spans

A line that closes a piece of work is also its span: it names itself and the span that started it, so the lines of
one `trace_id` rebuild the tree of what called what and where the time went, with no tracing library in the host
and no span store. The closing lines are the request line, the WebSocket session line and every `StepTimer` line;
each carries `span_id` and `parent_span_id`, 16 lowercase hex digits. Every other line, the lifespan lines, the
crash lines, asyncio's lines and uvicorn's lines among them, carries neither.

| Line | `span_id` | `parent_span_id` |
|---|---|---|
| request, session | the span logline opens for it, the one `traceparent` hands on | the incoming `traceparent`'s span id, `null` for a new trace |
| step | the span its block opened | the span current when the block was entered: the request's, the session's, the sender's inside a turn, or an outer step's |

The current span lives in OpenTelemetry's context, as the trace does; logline keeps no copy of it. A call a host
makes inside a step carries the step's span, so the callee's request line has the step's `span_id` as its
`parent_span_id`. That holds for the covered clients ([Outgoing calls](#outgoing-calls)) and for a call that passes
`outgoing_headers()` or `opentelemetry.propagate.inject`, and for a call a thread pool's work item makes. A
`websockets` client opened inside a step carries the step's span in its handshake, so the callee's session line has the
step's `span_id` as its `parent_span_id`.

### A turn on a WebSocket

A WebSocket session runs in one trace, and its turns need their own, each ending with it.
`unit_trace(traceparent)` runs a block, one turn, in the incoming trace or a new one, and detaches it when the
block ends, so the session's trace is current again. A valid `traceparent` is continued as it is: the turn has no
line of its own, so inside it, outside a step, the current span is the sender's. Without a valid one the turn starts
a new trace with a fresh span. An exception leaves the block
unchanged, and no later line of the socket carries the failed turn's `trace_id`. `current_traceparent()` returns the
`traceparent` value for a message: the step's inside a `StepTimer` block, else the turn's (the sender's span for a
valid incoming value), the session's outside a turn, `None` outside any trace.

The sending side puts the value in the message it sends:

```python
from logline import current_traceparent, unit_trace


async def speak(websocket, text):
    with unit_trace(None):
        await websocket.send_json({"text": text, "traceparent": current_traceparent()})
        ...
```

The receiving side runs the turn under the value it received, `None` when the message has none or a
malformed one, which starts a new trace as a request does. The block's own errors leave it with
the trace ended, so a handler outside the block logs a line with the session's `trace_id`, not the turn's; a host that needs the
failure line on its turn logs it inside the block:

```python
async for message in websocket.iter_json():
    with unit_trace(message.get("traceparent")):
        try:
            await handle_turn(message)
        except Exception:
            logger.exception("turn_failed")
            raise
```

`tracestate` is not carried in a message.

## Settings

| Variable | Effect |
|---|---|
| `LOG_LEVEL` | the root level: `debug`, `info`, `warning`, `error` or `critical`, case-insensitive; `info` when unset. Anything else, an empty value included, makes `configure` raise `ValueError` |
| `SERVICE_VERSION` | the `version` of every line; unset or empty makes it `null` |
| `OTEL_PROPAGATORS` | read by OpenTelemetry; unset or empty lets `configure` set the global propagator to W3C trace context, anything else leaves it as OpenTelemetry built it; `none` also stops the trace headers on outgoing requests ([Trace context](#trace-context)) |

## The host's duties

logline shows what the app logs; the app keeps its logs meaningful and safe:

- An exception message is logged as written. A secret in a message, or in a field the app logs, is the app's
  defect: it raises and logs ids, never tokens or contents.
- No `logging.config.dictConfig` after `configure`: it replaces the handlers `configure` installed.
- uvicorn runs with `log_config=None` (see below).
- `configure` is the first call of the process; a crash before it stays Python's text on stderr.
- A host's own `loop.set_exception_handler` wins over logline's coding, and `logline.check` cannot see it in the
  source. A host handler that falls back to `loop.default_exception_handler` gets the coded line.
- Close what holds a resource before the process ends: an exception raised in a cleanup while the interpreter
  shuts down stays Python's text on stderr ([Crashes and warnings](#crashes-and-warnings)).
- A `multiprocessing` child catches and logs its own failure.

`logline.check` judges them in a host's source (see [Checking a host's source](#checking-a-hosts-source)).

## Starting uvicorn

Call `configure` first, then give uvicorn `log_config=None` and no `log_level`: `uvicorn.run(app, log_config=None)`
or `uvicorn.Config(app, log_config=None)`. Under the uvicorn command line, call `configure` where the app
module is imported or in the app factory. `configure` routes uvicorn's loggers to the JSON handler and keeps
uvicorn's access log silent in every start mode, because the request line replaces it.

`configure` also rewrites every record on `uvicorn.error`, and the refusal line websockets writes there for an
upgrade that never reaches the app, into a literal event. Each keeps its level and carries no message text, process id, host, port or path. These events
and their `reason`, `status`, `task_count` and `error_type` values are public: renaming one is a breaking change.

| Event | Level | When | `reason` | Other field |
| --- | --- | --- | --- | --- |
| `server_bind.failed` | error | the server cannot bind its address | `port_taken` (address in use) or `os_error` | `error_type`: the `OSError` class |
| `http_request.refused` | warning | uvicorn refuses a request | `invalid_request` (not valid HTTP or HTTP/2), `unsupported_upgrade`, `websocket_library_missing` (an upgrade, and no WebSocket library is installed) or `concurrency_limit` (answered 503) | none |
| `asgi_app.contract_broken` | error | the app breaks the ASGI contract uvicorn checks | `response_not_started`, `response_not_completed` or `value_returned` (the app returned a value, which is left out) | none |
| `websocket_frame.refused` | error | a client's text frame is not UTF-8 | `invalid_utf8` | none |
| `app_lifespan.unsupported` | info | the app does not answer the lifespan scope | none | none |
| `asgi_app.failed` | error | the app raises in an HTTP request or a WebSocket, with the traceback in `exception` | none | `error_type`: the exception class |
| `websocket_upgrade.refused` | info | websockets answers a malformed WebSocket upgrade before any app task exists | none | `status`: the HTTP status |
| `server_process.started`, `server_process.finished` | info | the server process starts and ends | none | none |
| `server_process.limit_reached` | info | the server hit its request limit and stops | none | none |
| `server_listen.started` | info | the server listens, whatever the address form | none | none |
| `server_shutdown.started` | info | the server begins to stop | none | none |
| `server_shutdown.waiting` | info | the stop waits for work left | `connections` (an open connection) or `background_tasks` (a request the client left) | none |
| `server_shutdown.timed_out` | error | the graceful timeout ended and the server cancels the tasks left | none | `task_count`: the tasks cancelled |
| `uvicorn.unknown_record` | the record's level | a record on `uvicorn.error` whose template logline does not know | none | none |

An upgrade that reaches the app is never a `websocket_upgrade.refused` line: a denial response, a close before accept
and an app that returned without answering are `websocket.ended` lines (see
[The WebSocket session line](#the-websocket-session-line)), under uvicorn's default WebSocket protocol and under
`ws="wsproto"`, which writes no refusal record of its own. `configure` drops websockets' `connection open`, its `connection closed` (the session line or the refusal says the same, and a
connection that never became a WebSocket has no record to keep),
uvicorn's `ASGI callable returned without completing handshake.` and `... without sending handshake.`, and the
refusal websockets writes in the task of the app, because the session line says the same. uvicorn's older
`ws="websockets"` implementation writes that refusal from another task, so it still writes
`websocket_upgrade.refused` beside the session line for a denial. uvicorn's "Waiting for application startup" and
"Waiting for application shutdown" lines are dropped, because the next line says the same.

A record of a template logline does not know, such as one a newer uvicorn adds, is the line
`uvicorn.unknown_record` at its level, with no text and no traceback: a later release names it. The line the app's
exception writes keeps its traceback in `exception`; no other `uvicorn.error` line carries one.

### The app's startup and shutdown lines

`RequestLineMiddleware` writes these lines from the ASGI `lifespan` scope, on the logger `logline.lifespan`, so they
read the same under any ASGI server. They are public: renaming one is a breaking change.

| Event | Level | When | Other field |
| --- | --- | --- | --- |
| `app_startup.completed`, `app_shutdown.completed` | info | the app answered the phase with `complete` | none |
| `app_startup.failed`, `app_shutdown.failed` | error | the app answered the phase with `failed`, or raised after it received the phase's message, with the traceback in `exception` | `error_type`: the exception class |

A `failed` answer carries the traceback of the exception being handled when the app sends it, as Starlette and FastAPI
do; an app that sends `failed` outside an exception gets the line without `error_type` and `exception`. The
middleware forwards the answer without its free-text `message`, so the server writes none. This changes what the
server receives: hypercorn shows no message in its own error. An exception raised before the app received
`lifespan.startup` is the ASGI spec's "lifespan unsupported" and writes no line.

The completed lines follow `LOG_LEVEL`, as uvicorn's did: at `warning` or above they are not written. The failed
lines are at error.

A host without the middleware, or an app that has no lifespan, gets no `app_*` lines. Behind uvicorn the middleware
owns them: `configure` drops uvicorn's own startup and shutdown lines, so a host that does not wrap its app loses
them.

**Breaking:** the `logger` field of the four `app_*` lines moves from `uvicorn.error` to `logline.lifespan`. A host
that filters or routes on the old logger changes it; a host that reads the `event` needs no migration.

## Never logged

At the default level and above, no line carries credentials, tokens, cookies, header values, contents (bodies,
messages, prompts, transcripts, audio) or raw paths and query strings: logline writes none of them, and a host
adds none through a field, so it logs ids and the `trace_id`. At `LOG_LEVEL=debug`, DEBUG records of other
libraries pass as those libraries write them, and some of those write paths, query strings, header values or
frames; logline neither masks nor caps them. `configure` also does the following at every `LOG_LEVEL`; a cap only
raises the floor, so a stricter `LOG_LEVEL` silences the capped logger too:

- renders exceptions as text, never as a dict with local variables;
- sets `sys.excepthook`, `threading.excepthook`, `warnings.showwarning` and `sys.unraisablehook`, so that the process's
  death, a thread's death, a Python warning and an exception raised in an object's cleanup are one line each on stdout
  (`process.crashed` and `thread.crashed` on `logline.crash`, `python_warning.raised` on `py.warnings`,
  `object_cleanup.failed` on `logline.crash`), where Python would print plain text on stderr;
- wraps asyncio's default exception handler and filters the logger `asyncio`, so that a failure asyncio reports is a
  coded line and no text of asyncio's, which carries the exception's message, an object's repr and source paths;
  `logline.testing.configured` restores the four hooks and the asyncio handling;
- sets the `httpx`, `httpx2`, `httpcore` and `httpcore2` loggers to WARNING, because their INFO lines carry the full URL
  with its query; the callee's request line with the same `trace_id` covers the call;
- sets the `mcp` tree to WARNING, because `mcp.client.streamable_http` logs the session id, which works like a bearer
  token, and a new INFO line must not leak;
- sets the `botocore` tree to WARNING, because `botocore.credentials` logs the absolute path of the credentials file at INFO;
- sets `sqlalchemy.dialects.postgresql` to WARNING, because it logs the text of a server notice;
- sets `piper` to ERROR, because it writes spoken text and phonemes at WARNING;
- drops uvicorn's WebSocket connection lines, which carry the raw path with its query;
- drops records below INFO on `uvicorn.error`, because websockets writes the request path, every
  header and every frame there at DEBUG.

## Fleet standard: logging through logline

The rules for what a covered host logs and how. What a line never carries is [Never logged](#never-logged).

### Who is covered, from when

- Every fleet repository whose code runs as a deployed process: a server, a worker or an app.
- Every library whose code runs on a hop of such a process.
- Not covered: forks of third-party projects, pure command-line tools, and code that runs in a person's browser,
  which is no deployed process; the server end of the connection it opens logs that hop.
- New code complies at once; existing repositories adopt now.
- `service` is one name per deployed process: the app's main process takes the app's name, every other
  process `<app>-<role>`, such as `presentator-worker`.
- The rules a machine can check: a released logline tag, a literal event name of the form below, no
  value formatted into a message, no field from the deny list, and no logging setup of the
  host's own. `logline.check` judges them (see [Checking a host's source](#checking-a-hosts-source)). The
  record drops a field on the deny list.
- The deny list, the event-name pattern, the calls and assignments that are a host's own logging setup, the
  record's base keys and the keys of a closing line are the data file `src/logline/standard.json` in the logline package, which owns
  them; `logline.standard` reads it.
- Whether the log points, levels and single errors are right is judged by a scheduled audit.

### Where to log

| Point | Level |
|---|---|
| Inbound HTTP request (the middleware writes it) | info |
| Unit of work that is not an HTTP request (a turn, a job run, a queue message), one line at its end | info; error when it failed |
| Step inside a unit (each outbound call to another process, each wait or compute stage the caller waits on), one line at its end | info; warning when it failed and the unit went on; error when the unit failed with it |
| State change of a domain object | info |
| Refusal, with `reason` | info |
| Background run | info; a run that found no work writes no line, a run with work writes one line with its counts, a failing repeating run writes its first failure (warning) and its recovery (info) with `failed_run_count` |
| Process start, ready and stop | info |
| Error, once, where the exception stops | error |

Not logged: start lines of short steps, lines per item, per frame, per chunk or per token, a second request
line, the same error at each layer.

### Event names

`event` is a literal `<noun>.<verb_past>` (the pattern is `event_name_pattern` in `standard.json`), with no service prefix and no values, such as
`turn.ended`, `hearing.ended` or `sign_in.refused`. The request line is `request.ended` with `outcome`
(`REQUEST_EVENT`), and the WebSocket session line is `websocket.ended` (`WEBSOCKET_EVENT`).

- `.ended` closes a timed step and always carries `duration_ms`, `outcome`, `span_id` and `parent_span_id`; a
  hand-written `.ended` line carries no span, so a step is written by `StepTimer`.
- `.started` is only for a process or for work that runs for minutes.
- `.failed` is an error outside a timed step.

One event per step, whatever its outcome.

### Crashes and warnings

`configure` replaces Python's plain-text stderr output of four failures, and asyncio's free-text records, with one
coded line each on stdout, so a host installs no hook of its own. These events and their `error_type` values are public: renaming one is a breaking change.
A dying process exits with status 1 as before. Every `.failed` line carries the traceback in `exception` and the
exception class in `error_type`, and none carries a message text, an object's repr or a path outside the traceback.

| Event | Logger | Level | When | `error_type` |
| --- | --- | --- | --- | --- |
| `process.crashed` | `logline.crash` | critical | the process ends on an uncaught exception, with the traceback in `exception` | the exception class |
| `thread.crashed` | `logline.crash` | critical | a thread ends on an uncaught exception other than `SystemExit`, with the traceback in `exception` | the exception class |
| `python_warning.raised` | `py.warnings` | warning | Python issues a warning; its message text is written nowhere | the warning category |
| `object_cleanup.failed` | `logline.crash` | error | an exception is raised in `__del__`, a weakref callback or a finalizer, where nothing can catch it; no message and no repr of the object | the exception class |
| `asyncio_task.failed` | `asyncio` | error | a task or future whose exception nobody retrieved, a task that raised during `asyncio.run()` shutdown, a shielded future's exception, a task group's task that errored out after its parent completed, or an `asyncio.start_server` `client_connected_cb` that raised | the exception class |
| `asyncio_callback.failed` | `asyncio` | error | any other asyncio record that carries an exception, such as an exception in a callback, a fatal transport error or an exception in the default exception handler | the exception class |
| `asyncio.unknown_record` | `asyncio` | the record's own | every asyncio record from INFO up that carries no exception, such as a task destroyed while pending; no text and no traceback | none |

asyncio's records below INFO pass as asyncio writes them. Each failure is its own line; repeats are not folded.

The `asyncio` lines carry the `trace_id` of the task or callback that failed, where asyncio hands logline its
context: a task started inside a request, whose exception nobody retrieves, writes its line with that
request's `trace_id`, also when it is collected while another request runs. A plain future, a failure that names no
task (a `client_connected_cb`), a record asyncio writes outside its exception handler and an `object_cleanup.failed`
line carry `trace_id` `null`, never the trace that happens to be current.

Named gaps, each with its owner:

- An exception raised in a cleanup after logline's exit handler ran, such as a module global's `__del__` at
  interpreter shutdown, stays Python's text on stderr: a Python-level hook is told of none there, and it would hide
  the failure. The host closes what holds a resource before the process ends.
- A crash in a `multiprocessing` child is printed by Python's own bootstrap, not through `sys.excepthook`, even
  when the child called `configure`. The host catches and logs the child's failure.
- A crash before `configure` runs stays Python's text; the host calls `configure` first.
- Under an event loop that is not asyncio's own, such as uvloop, asyncio's handler is bypassed: every failure is an
  `asyncio_callback.failed` line with `trace_id` `null`, named by the filter alone. A host that adopts uvloop
  pulls the work to name its failures and carry their trace.
- An end the process cannot see (an OOM kill, SIGKILL) writes no line. The platform owns it: today the pod's last
  termination reason shows it, and one kill raises no alert.

**Breaking:** asyncio's records under `configure` become the coded events above, so a host that reads asyncio's text
from the log sees a literal `event`, and an unraisable exception writes a line on stdout instead of Python's text on
stderr. Migration: none required; a host whose tests read asyncio's log text or an unraisable's stderr sees the
coded line.

### Shared fields

The same name and type in every app. A new field is an id, a number whose name carries its unit, or a code
from a closed set.

| Field | Type | Use |
|---|---|---|
| `duration_ms` | number | the step's start to the line's `ts`, on a monotonic clock, each process measuring its own |
| `first_output_ms` | number | the time to the first output, streaming steps only |
| `outcome` | `ok`, `refused`, `failed` or `cancelled` | on every `.ended` line |
| `span_id`, `parent_span_id` | 16 hex digits, `null` for none | the span a closing line is and the span that started it ([Spans](#spans)) |
| `reason` | code from a closed set in the app | why it was refused, failed or ended |
| `error_type` | string | the exception's class name |
| `status` | integer | the HTTP status of an outbound call, or the status the client got for a WebSocket upgrade the app did not accept |
| `close_code` | integer | the close code of a WebSocket session, from the closed set the protocol defines |
| `sub` | string | the person; never `subject_id` or `user_id` |
| `<entity>_id` | string | a durable id |
| `<noun>_count`, `_bytes`, `_tokens`, `_ms` | number | the unit is in the name |

### Levels

- `info`: something happened as designed, a refusal included.
- `warning`: degraded, but the unit went on.
- `error`: a unit or step failed and its unit did not go on.
- `critical`: the process cannot go on, or data is at risk.
- `debug`: developer detail.

### The helpers

`Outcome` is the one owner of the four `outcome` values (`ok`, `refused`, `failed`, `cancelled`), a `StrEnum`
that writes as its text. `StepTimer` writes a step's one `.ended` line, as `with` or `async with`:

```python
import logging

from logline import StepTimer

_LOGGER = logging.getLogger(__name__)


async def ask_model(client, request):
    async with StepTimer("model_request.ended", _LOGGER, agent_id=request.agent_id) as step:
        async for chunk in client.stream(request):
            step.first_output()
            yield chunk
```

- The line goes through the standard-library logger the caller names, a library's own, so a host without
  `configure` gets an ordinary record under its own logging setup, and one with `configure` gets the JSON
  line with the running request's `trace_id`. A sync `with` on a worker thread (`asyncio.to_thread`, a sync
  endpoint, `run_in_executor`) carries the request's `trace_id` too.
- The timer starts when it is constructed, not when the block is entered. A step that began earlier, such as
  a wait whose start a host keeps as a `time.perf_counter()` value, passes it as the keyword `started_at`,
  and `duration_ms` and `first_output_ms` count from it; a value later than `time.perf_counter()` raises
  `ValueError` at construction. `started_at` is no step field name: `add` drops it. The line carries the
  step's id fields (scalars), `duration_ms` on the request line's clock and rounding, and `outcome`; it is
  written in `finally`, so a failed or cancelled step still leaves it.
- The timer owns the keys `duration_ms`, `outcome`, `reason`, `first_output_ms`, `error_type`, `started_at`,
  `span_id` and `parent_span_id`: a field of one of those names is dropped. A field named like an attribute of `logging.LogRecord` (`name`, `module`,
  `process`, `thread`, `args`, `message`, ...) raises `ValueError` when the timer is constructed, because the
  standard library refuses it as an `extra` key.
- A block that ends normally is `ok`. `step.reason(code)` gives that `ok` line a `reason`, such as `silence` for
  a wait that ended because the speaker stopped; a later `refuse` or `fail` replaces it, and a failed or
  cancelled end by exception carries none of it. `step.refuse(reason)` makes it `refused` with `reason`, a code from the
  app's closed set. `step.first_output()` adds `first_output_ms`, from the first call only.
- An `Exception` makes it `failed` with `error_type`, the class name, and propagates unchanged. The line
  never carries the message or a traceback: the layer where the exception stops logs it. After
  `step.refuse(reason)`, an exception that leaves the block makes it `failed` with `error_type` and without
  the refusal's `reason`.
- Any other `BaseException` (`asyncio.CancelledError`, `GeneratorExit`) makes it `cancelled` without
  `error_type`, and propagates.
- `step.add(**fields)` sets more fields before the line is written, such as a count known only at the end. It
  follows the construction rules: a name of a `logging.LogRecord` attribute raises `ValueError`, which inside a
  block fails that block like any other `Exception`; a timer key is dropped, and a later value for the same key,
  the constructor's included, replaces the earlier one.
- `step.fail(reason)` ends a block that ends normally as `failed` with `reason`, a code from the app's closed
  set, and without `error_type`. When an exception leaves the block after `fail`, the line is `failed` with
  both `error_type` and `reason`. `fail` wins over `refuse` in either order. A `cancelled` step carries
  neither, whatever `fail` said.
- `step.end()` writes the line now, as a block that ends normally would, for a step that no `with` block
  surrounds, such as a wait that spans WebSocket events. `step.end_cancelled()` writes it now as `cancelled`,
  at INFO, without `error_type` and `reason`, as a block that a cancellation leaves would: for a step whose
  task is cancelled before a block surrounds it, ended from the task's done callback. `step.end_failed(error)`
  writes it now as a block that the `Exception` `error` leaves would: `failed` with `error_type`, beside `reason`
  when `fail` named one, at ERROR or at WARNING after `handled()`; for a step ended by an exception that no block
  caught, such as a stream's run summary. It neither raises nor keeps `error`. The line is written once: after any
  of them, a later `end()`, `end_cancelled()`, `end_failed()` or block exit writes nothing more.
- Entering the block opens the step's span under the current one, and it stays current until the block ends: a step
  inside it, `outgoing_headers()` and `current_traceparent()` name it. The line carries its `span_id` and
  `parent_span_id` and the `trace_id` of the trace it was opened in, wherever it is written, so a block that ends in
  another task or turn still writes all three from one trace. When the block ends, the previous span is current
  again, unless the block ends in another context than it began in, whose current span stays as it is. A step no
  block surrounds takes a fresh span when `end()`, `end_cancelled()` or `end_failed()` writes its line, under the
  current span; one that ends inside its block writes the block's span. A step whose line `end()`,
  `end_cancelled()` or `end_failed()` already wrote opens no span when it is then entered as a block: a call or
  step inside names the span the block ran under.
  A step outside any trace has no span: `span_id` and `parent_span_id` are `null`, and it starts no trace.
- The line is at INFO. `step.degraded()` makes it a warning, such as a `refused` call the unit retried. A
  `failed` step is at ERROR, or at WARNING after `step.handled()` when its unit goes on; a step that is
  degraded and failed unhandled stays at ERROR.
- A step inside an async generator keeps its span current between two `yield`s, so a call the generator makes
  names the step. The cost is on the consumer: its steps between two `yield`s name the generator's step as their
  parent, and after a `break` without `aclosing` the dropped step stays the parent of the consumer's later steps
  until the unit ends. The dropped step's own line is still written with its own trace and span at garbage
  collection. A host wraps such a generator in `contextlib.aclosing` so the line is written where the generator is
  used:

```python
from contextlib import aclosing

async with aclosing(ask_model(client, request)) as chunks:
    async for chunk in chunks:
        ...
```

### Errors

- The layer where the exception stops writes the one line; a layer that re-raises writes none.
- A failed timed step is its own `.ended` line with `outcome` `failed` (`StepTimer` writes it), and `error_type`
  when an exception ended it; `fail(reason)` ends it with `reason` and no `error_type`.
- A traceback goes only on an exception the code did not expect.
- A step line is written in `finally`, so a failed or cancelled step still leaves it.

### Correlation

`trace_id` is the one correlation id. Every outbound call and every message of a unit carries it
([Trace context](#trace-context)). The service where a turn begins starts the turn's trace, and every later
message of the turn carries it, a call back to that service included.

### A library on a hop

- A host owns a process's entry point; a library on a hop is code imported into it; one repository may hold both.
- Only the host calls `configure` and adds the middleware; a cap on another library's logger lives in `configure`.
- A library logs through a standard-library logger named under its package: the literal event as the message,
  fields in `extra=`, timed steps through logline's `StepTimer` on that logger. It never calls `configure` or structlog.
- The code in the same process that makes a call or runs a wait writes its step line; its caller writes no line
  for the same step.
- Whoever takes a unit off a wire or a timer, host or library, writes its unit line and starts its trace through
  logline's helper.
- A library's event names are public: renaming one is **Breaking** on its release page.

### Deviations

A deviation is a request that names its enduring harm. logline's head reviews it and only the operator rules
it. A ruled deviation is recorded here; none is recorded today.

### The target picture

One spoken turn across three services. After the first line, `version`, `logger` and the full `trace_id` are
cut for width only.

```json
{"ts":"2026-11-15T16:02:11.640113Z","level":"info","service":"speech","version":"1.9.0","logger":"speech_service.live_session","event":"turn_end_wait.ended","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","duration_ms":640.0,"outcome":"ok","reason":"silence","item_id":"item_81c2"}
{"ts":"2026-11-15T16:02:11.982260Z","level":"info","service":"speech","event":"hearing.ended","trace_id":"4bf92f35","duration_ms":342.1,"outcome":"ok","item_id":"item_81c2","audio_ms":3120,"partial_count":2}
{"ts":"2026-11-15T16:02:11.991402Z","level":"info","service":"hopin","event":"transcript_relay.ended","trace_id":"4bf92f35","duration_ms":8.6,"outcome":"ok","room_id":"room_5d0e","agent_id":"agent_19"}
{"ts":"2026-11-15T16:02:12.066318Z","level":"info","service":"personas","event":"turn_setup.ended","trace_id":"4bf92f35","duration_ms":71.3,"outcome":"ok","agent_id":"agent_19"}
{"ts":"2026-11-15T16:02:13.310247Z","level":"info","service":"personas","event":"model_request.ended","trace_id":"4bf92f35","duration_ms":1243.9,"first_output_ms":418.2,"outcome":"ok","status":200,"input_tokens":5210,"output_tokens":64}
{"ts":"2026-11-15T16:02:15.202431Z","level":"info","service":"speech","event":"speak.ended","trace_id":"4bf92f35","duration_ms":1876.4,"first_output_ms":186.5,"outcome":"ok","audio_ms":4100,"character_count":61}
{"ts":"2026-11-15T16:02:15.204018Z","level":"info","service":"speech","event":"request.ended","trace_id":"4bf92f35","outcome":"ok","method":"POST","route":"/v1/audio/speech","status":200,"duration_ms":1878.0,"sub":"9d2c4b1e-6f0a-4c3d-8a77-1b5e0c9f2d41","ip":"10.42.1.17"}
{"ts":"2026-11-15T16:02:15.217306Z","level":"info","service":"hopin","event":"speak.ended","trace_id":"4bf92f35","duration_ms":1893.2,"first_output_ms":201.7,"outcome":"ok","room_id":"room_5d0e","agent_id":"agent_19"}
{"ts":"2026-11-15T16:02:15.222540Z","level":"info","service":"hopin","event":"request.ended","trace_id":"4bf92f35","outcome":"ok","method":"POST","route":"/mcp","status":200,"duration_ms":1899.5,"sub":"c81f0a55-2b9e-4d17-9e3a-7f6d21a4b0c2","ip":"10.42.2.8"}
{"ts":"2026-11-15T16:02:15.230077Z","level":"info","service":"personas","event":"turn.ended","trace_id":"4bf92f35","duration_ms":3235.0,"outcome":"ok","agent_id":"agent_19","model_request_count":1,"tool_call_count":1}
```

One web request with an outbound call and one error:

```json
{"ts":"2026-11-15T16:07:40.118204Z","level":"info","service":"skat","event":"token_exchange.ended","trace_id":"0af7651916cd43dd8448eb211c80319c","duration_ms":84.2,"outcome":"ok","status":200}
{"ts":"2026-11-15T16:07:40.129733Z","level":"error","service":"skat","event":"sign_in.failed","trace_id":"0af76519","error_type":"OperationalError","exception":"Traceback (most recent call last):\n  ...\nsqlalchemy.exc.OperationalError: connection refused"}
{"ts":"2026-11-15T16:07:40.131090Z","level":"info","service":"skat","event":"request.ended","trace_id":"0af76519","outcome":"failed","method":"GET","route":"/auth/callback","status":500,"duration_ms":96.7,"sub":null,"ip":"203.0.113.9"}
```

Why this log is the target:

- **What happened:** each line is one finished thing under a fixed event name, and the ten lines are the turn's
  whole path through three services under one `trace_id`.
- **Where the time went:** `ts` minus `duration_ms` is a step's start, so the gaps between services are the
  network. The turn takes about 2.5 s from the person's silence to the first audio: 640 ms wait, 342 ms hearing,
  9 ms relay, 71 ms setup, 1244 ms model and 202 ms to first audio.
- **Both sides of a hop:** `speak.ended` in speech and in hopin differ by 17 ms, so the relay is not the problem.
- **What failed:** one error line names the exception's class and the frame that raised it, and the request line
  shows the 500.
- **What it leaves out:** transcripts, spoken text, prompts, tokens, paths, queries, start lines and per-chunk
  lines. Ids point to the content instead.

## Testing a host

`logline.testing.configured(service)` is a context manager for a host's in-process tests. It calls
`configure(service)` and on exit restores the process's logging as it was before: the root logger's handlers
and level, the handlers, filters, levels and propagation of the uvicorn, HTTP client, `logline.request` and
capped library (`mcp`, `botocore`, `sqlalchemy.dialects.postgresql`, `piper`) loggers, structlog's configuration and the
stdlib logger class its factory installs, the process's and the threads' exception hooks, the unraisable-exception
hook and the warning display, OpenTelemetry's global propagator, whether outgoing requests and thread pool work items
carry the trace and whether asyncio's failures are coded. Inside the block a covered client's request, and a work item of a thread pool, carries the current trace
([Outgoing calls](#outgoing-calls)), so a test that asserts a request's exact headers sees it. After the block requests
and work items carry the trace only if they did before it; the carriers stay on the HTTP clients' methods and on
`ThreadPoolExecutor.submit`, which `configured` never reassigns, so a wrapper the host or a library put on one survives.
The module imports no test framework.

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

## Checking a host's source

`logline.check.check(source, path)` returns the rows one Python source breaks, as a tuple of
`Finding(path, line, column, rule)`, in source order; `line` and `column` count from 1, the column in characters. It is a pure function
over text: it never imports or runs the checked code, and a source that does not parse raises `SyntaxError`.
`path` only labels the rows. Quality's gate runs it over every host whose root `pyproject.toml` declares logline, with the rules of the logline
tag the gate pins, not the tag the host pins; it is report-only until quality#914 makes it red.
Importing it runs `logline/__init__.py`, so `structlog` and
`opentelemetry-api` must be installed: the check needs only the standard library, but a lazy `__init__` would make every public name a deferred import, so it stays eager. `Finding`, `Rule` and `check` are public, and renaming a `Rule` code is
a breaking change.

| `Rule` | Broken when |
| --- | --- |
| `event_not_literal` | the event of a logging call or of `StepTimer(...)` stands for no string literal: an f-string, a formatted text, or a name the check cannot resolve to one (see below) |
| `event_name_shape` | the event is a literal that does not match the event-name pattern |
| `message_args` | a standard-library logging call passes positional arguments after the event |
| `denied_field` | a field on the deny list is a keyword of a structlog call, of `.bind(...)`, of `bind_contextvars(...)`, of `StepTimer(...)` or `step.add(...)`, or a literal key of an `extra=` dict |
| `own_setup` | the source calls one of `own_setup_calls` in `standard.json` (`logging.basicConfig`, `structlog.configure`, ...) or assigns `sys.excepthook`, `threading.excepthook` or `sys.unraisablehook` |
| `uvicorn_log_config` | `uvicorn.run`, `uvicorn.main.run` or `uvicorn.Config` is called without `log_config=None` |
| `uvicorn_log_level` | `uvicorn.run`, `uvicorn.main.run` or `uvicorn.Config` is called with a `log_level` |
| `uvicorn_options_hidden` | one of them is called with a `**` argument, a `*` argument or a positional argument after the app, which hide its options |
| `hand_written_ended` | a logging call's literal event (or the literal its same-module constant or member stands for), or its one-hop event parameter when some call passes such a literal, matches `event_name_pattern` of `standard.json` and ends in `closing_event_suffix` of `standard.json` (`.ended`), which only `StepTimer` writes with its span; a `StepTimer` event is no logging call and is not reported |

A logging call is `debug`, `info`, `warning`, `error`, `exception`, `critical` or `log` (the event is its
second argument) on a logger the same file binds from `logging.getLogger`, `structlog.get_logger`,
`structlog.stdlib.get_logger` or `.bind(...)`, on the `logging` module, or on a parameter or attribute
annotated `logging.Logger` or structlog's `BoundLogger`; imports are resolved, aliases included. A call gets
at most one event row.

An event is a string literal, or a name bound once at module level of the same file by an annotated assignment
of a string literal whose annotation is `Final` or `Final[str]` (from `typing` or `typing_extensions`), or
`Class.MEMBER` of a module-level `StrEnum` class of the same file whose body assigns `MEMBER` a string
literal. The check judges that literal, its shape and `.ended` suffix, as it judges one at the call.

A name an enclosing function binds itself, a name bound twice or without `Final`, a member assigned `auto()`,
and a constant or member imported from another file stay `event_not_literal`. An event may be a parameter of a
function in the same file when every call of that function in the file passes a matching literal, or such a
constant or member; the check follows one hop and never crosses a file. Setup on a named library logger, such
as `logging.getLogger("httpx").addFilter(...)` or `setLevel`, is not own setup.

What the check does not see, so a clean result is not a proof:

- A logger handed in without an annotation, and one bound in another file.
- An event constant or member imported from another file, which reads as `event_not_literal`.
- An `extra=` given as a variable, and any `**fields`; the record still drops a denied field at runtime.
- A logging function imported by name, such as `from logging import warning`.
- An event passed as a keyword (`event=`, `msg=`); only the positional event is read.
- An annotated or augmented assignment to `sys.excepthook`; only a plain assignment is own setup.
- A host's own `loop.set_exception_handler`, which wins over logline's coding of asyncio's failures.
- A parameter reassigned before it is logged; the check judges the literal each caller passes.
- A `@staticmethod` whose event parameter is called through `self`, which reads as `event_not_literal`, a false
  positive.
- The uvicorn command line's `--log-level` in a Dockerfile or Compose file.

## Versioning

The version follows [semantic versioning](https://semver.org/). From 1.0 the public names are stable within a major:
only renaming or removing one, or changing its signature, is a major, and a patch release never breaks one. The one
semver exception: a change to the record, to an event name, to the headers `configure` puts on outgoing requests,
to the trace it gives a thread pool's work or to `configure`'s policy touches hosts only, so it is a
minor release with a **Breaking** note in the [release pages](https://github.com/overnightworks/logline/releases),
because hosts pin a tag and take it deliberately. The public modules are `logline`, `logline.check`, `logline.testing` and `logline.trace`; a public name is one that a
public module's `__all__` lists, and everything else may change in any release.

## Development

[AGENTS.md](AGENTS.md) holds the repository's commands and rules. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
says which module owns what; [docs/VISION.md](docs/VISION.md) says what logline is for.

## Licence

MIT, see [LICENSE](LICENSE).
