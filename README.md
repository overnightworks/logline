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

A library on a hop declares `overnightworks-logline` as a version range by name and puts its git URL only in
its dev group; the host pins the git tag, so one logline runs in the process and a tag outside the range is
refused. A library that pins a tag by git URL forces every host to pin the same tag. quality's fleet package
standard owns the rule and its check.

```toml
# the library
dependencies = ["overnightworks-logline>=0.3,<0.4"]

[dependency-groups]
dev = ["overnightworks-logline"]

[tool.uv.sources]
overnightworks-logline = { git = "https://github.com/overnightworks/logline", tag = "vX.Y.Z", group = "dev" }
```

The host's `dependencies` stay as above.

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
`logline.REQUEST_EVENT` (`request.ended`) and `outcome`, `method`, `route`, `status`, `duration_ms`, `sub` and
`ip`:

- `method` is one of `CONNECT`, `DELETE`, `GET`, `HEAD`, `OPTIONS`, `PATCH`, `POST`, `PUT` and `TRACE`, and any other
  token is `_OTHER`, so the field takes a closed set of values.
- `outcome` is `failed` when the app raised an `Exception` or the status is 5xx, `cancelled` when the app raised
  another `BaseException` (a cancellation), and `ok` otherwise, a 4xx status included: a refused request is
  the app's answer, not a failure. Such a `BaseException` still reaches the server, which answers 500 before
  the response starts, so the line then reads `cancelled` with `status` 500; once the response has started, the
  line keeps the status already sent, such as 200, with `cancelled`: read the two together. The line is written
  when the response ends, so an exception raised after that (in a background task) leaves the line's `outcome`
  and `status` as the response wrote them. A host compares `event` with `REQUEST_EVENT`, never with the literal.

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

### A turn on a WebSocket

A WebSocket connection is one long-lived task, so its turns need their own trace, and each must end with
it. `unit_trace(traceparent)` runs a block, one turn, in the incoming trace or a new one, with a fresh span
id, and detaches it when the block ends. An exception leaves the block unchanged, and no later line of the
socket carries the failed turn's `trace_id`. `current_traceparent()` returns the `traceparent` value for a
message, `None` outside a trace.

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
the trace ended, so a handler outside the block logs a line with no `trace_id`; a host that needs the failure
line on its turn logs it inside the block:

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

## The host's duties

logline shows what the app logs; the app keeps its logs meaningful and safe:

- An exception message is logged as written. A secret in a message, or in a field the app logs, is the app's
  defect: it raises and logs ids, never tokens or contents.
- No `logging.config.dictConfig` after `configure`: it replaces the handlers `configure` installed.
- uvicorn runs with `log_config=None` (see below).

`logline.check` judges them in a host's source (see [Checking a host's source](#checking-a-hosts-source)).

## Starting uvicorn

Call `configure` first, then give uvicorn `log_config=None` and no `log_level`: `uvicorn.run(app, log_config=None)`
or `uvicorn.Config(app, log_config=None)`. Under the uvicorn command line, call `configure` where the app
module is imported or in the app factory. `configure` routes uvicorn's loggers to the JSON handler and keeps
uvicorn's access log silent in every start mode, because the request line replaces it.

`configure` also rewrites uvicorn's own records on `uvicorn.error`, and the refusal line websockets writes there,
into literal events. Each keeps its level and carries no message text, process id, host, port or path. These events
and their `reason`, `status` and `error_type` values are public: renaming one is a breaking change.

| Event | Level | When | `reason` | Other field |
| --- | --- | --- | --- | --- |
| `server_bind.failed` | error | the server cannot bind its address | `port_taken` (address in use) or `os_error` | `error_type`: the `OSError` class |
| `http_request.refused` | warning | uvicorn refuses a request | `invalid_request` (not valid HTTP) or `unsupported_upgrade` | none |
| `asgi_app.failed` | error | the app raises in an HTTP request or a WebSocket, with the traceback in `exception` | none | `error_type`: the exception class |
| `websocket_upgrade.refused` | info | websockets answers a WebSocket upgrade with an HTTP status other than 101 | none | `status`: the HTTP status |
| `websocket_handshake.failed` | error | the app returned without answering the WebSocket upgrade | `handshake_incomplete` | none |
| `server_process.started`, `server_process.finished` | info | the server process starts and ends | none | none |
| `server_listen.started` | info | the server listens, whatever the address form | none | none |
| `server_shutdown.started` | info | the server begins to stop | none | none |

A refused upgrade is one `websocket_upgrade.refused` line and no error line: uvicorn's websockets-sansio protocol
also reports an upgrade the app answered with a denial response as an app that returned without completing the
handshake, and `configure` drops that line when the same connection already logged the refusal. An app that
returns without answering the upgrade gets `websocket_handshake.failed`, and then `websocket_upgrade.refused` with
status 500, the answer uvicorn sends in its place. The two lines are linked only when they come from one task of
one connection, which holds for uvicorn's default WebSocket protocol; the refusal line is read at any `LOG_LEVEL`,
so a root level above INFO drops both. uvicorn's "Waiting for application startup" and "Waiting for application
shutdown" lines are dropped, because the next line says the same.

Under `ws="wsproto"` uvicorn writes no refusal record: a denial response, a close before accept and an unanswered
upgrade produce no `websocket_upgrade.refused` line, and `websocket_handshake.failed` is still written.

Any other uvicorn record passes as uvicorn writes it.

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
- sets `sys.excepthook`, `threading.excepthook` and `warnings.showwarning`, so that the process's death, a thread's death and a
  Python warning are one line each on stdout (`process.crashed` and `thread.crashed` on `logline.crash`, `python_warning.raised`
  on `py.warnings`), where Python would print plain text on stderr; `logline.testing.configured` restores all three;
- sets the `httpx`, `httpx2`, `httpcore` and `httpcore2` loggers to WARNING, because their INFO lines carry the full URL
  with its query; the callee's request line with the same `trace_id` covers the call;
- sets the `mcp` tree to WARNING, because `mcp.client.streamable_http` logs the session id, which works like a bearer
  token, and a new INFO line must not leak;
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
- The deny list, the event-name pattern, the calls and assignments that are a host's own logging setup and
  the record's base keys are the data file `src/logline/standard.json` in the logline package, which owns
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
(`REQUEST_EVENT`).

- `.ended` closes a timed step and always carries `duration_ms` and `outcome`.
- `.started` is only for a process or for work that runs for minutes.
- `.failed` is an error outside a timed step.

One event per step, whatever its outcome.

### Crashes and warnings

`configure` replaces Python's plain-text stderr output of three failures with one line on stdout, so a host installs
no hook of its own. These events and their `error_type` values are public: renaming one is a breaking change.
A dying process exits with status 1 as before.

| Event | Logger | Level | When | `error_type` |
| --- | --- | --- | --- | --- |
| `process.crashed` | `logline.crash` | critical | the process ends on an uncaught exception, with the traceback in `exception` | the exception class |
| `thread.crashed` | `logline.crash` | critical | a thread ends on an uncaught exception other than `SystemExit`, with the traceback in `exception` | the exception class |
| `python_warning.raised` | `py.warnings` | warning | Python issues a warning; its message text is written nowhere | the warning category |

### Shared fields

The same name and type in every app. A new field is an id, a number whose name carries its unit, or a code
from a closed set.

| Field | Type | Use |
|---|---|---|
| `duration_ms` | number | the step's start to the line's `ts`, on a monotonic clock, each process measuring its own |
| `first_output_ms` | number | the time to the first output, streaming steps only |
| `outcome` | `ok`, `refused`, `failed` or `cancelled` | on every `.ended` line |
| `reason` | code from a closed set in the app | why it was refused, failed or ended |
| `error_type` | string | the exception's class name |
| `status` | integer | the HTTP status of an outbound call, or the status an app answered a WebSocket upgrade with |
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
  endpoint) carries the request's `trace_id` too.
- The timer starts when it is constructed, not when the block is entered. A step that began earlier, such as
  a wait whose start a host keeps as a `time.perf_counter()` value, passes it as the keyword `started_at`,
  and `duration_ms` and `first_output_ms` count from it; a value later than `time.perf_counter()` raises
  `ValueError` at construction. `started_at` is no step field name: `add` drops it. The line carries the
  step's id fields (scalars), `duration_ms` on the request line's clock and rounding, and `outcome`; it is
  written in `finally`, so a failed or cancelled step still leaves it.
- The timer owns the keys `duration_ms`, `outcome`, `reason`, `first_output_ms`, `error_type` and
  `started_at`: a field of one of those names is dropped. A field named like an attribute of `logging.LogRecord` (`name`, `module`,
  `process`, `thread`, `args`, `message`, ...) raises `ValueError` when the timer is constructed, because the
  standard library refuses it as an `extra` key.
- A block that ends normally is `ok`. `step.refuse(reason)` makes it `refused` with `reason`, a code from the
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
  task is cancelled before a block surrounds it, ended from the task's done callback. The line is written
  once: after either, a later `end()`, `end_cancelled()` or block exit writes nothing more.
- The line is at INFO. `step.degraded()` makes it a warning, such as a `refused` call the unit retried. A
  `failed` step is at ERROR, or at WARNING after `step.handled()` when its unit goes on; a step that is
  degraded and failed unhandled stays at ERROR.
- An async generator that is dropped without `aclose()` runs its `finally` at garbage collection, under
  whatever trace is current then, so its step line can carry another unit's `trace_id`. A host wraps such a
  generator in `contextlib.aclosing` so the line is written where the generator is used:

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
capped library (`mcp`, `sqlalchemy.dialects.postgresql`, `piper`) loggers, structlog's configuration and the
stdlib logger class its factory installs. The module imports no test framework.

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
`path` only labels the rows. Once quality#889 lands, the fleet's quality gate runs it over every host, with the rules of the logline
tag the gate pins, not the tag the host pins; until then nothing runs it. Importing it runs `logline/__init__.py`, so `structlog` and
`opentelemetry-api` must be installed: the check needs only the standard library, but a lazy `__init__` would make every public name a deferred import, so it stays eager. `Finding`, `Rule` and `check` are public, and renaming a `Rule` code is
a breaking change.

| `Rule` | Broken when |
| --- | --- |
| `event_not_literal` | the event of a logging call or of `StepTimer(...)` is no string literal, an f-string or a formatted text included |
| `event_name_shape` | the event is a literal that does not match the event-name pattern |
| `message_args` | a standard-library logging call passes positional arguments after the event |
| `denied_field` | a field on the deny list is a keyword of a structlog call, of `.bind(...)`, of `bind_contextvars(...)`, of `StepTimer(...)` or `step.add(...)`, or a literal key of an `extra=` dict |
| `own_setup` | the source calls one of `own_setup_calls` in `standard.json` (`logging.basicConfig`, `structlog.configure`, ...) or assigns `sys.excepthook` or `threading.excepthook` |
| `uvicorn_log_config` | `uvicorn.run`, `uvicorn.main.run` or `uvicorn.Config` is called without `log_config=None` |
| `uvicorn_log_level` | `uvicorn.run`, `uvicorn.main.run` or `uvicorn.Config` is called with a `log_level` |
| `uvicorn_options_hidden` | one of them is called with a `**` argument, a `*` argument or a positional argument after the app, which hide its options |

A logging call is `debug`, `info`, `warning`, `error`, `exception`, `critical` or `log` (the event is its
second argument) on a logger the same file binds from `logging.getLogger`, `structlog.get_logger`,
`structlog.stdlib.get_logger` or `.bind(...)`, on the `logging` module, or on a parameter or attribute annotated
`logging.Logger` or structlog's `BoundLogger`; imports are resolved, aliases included. A call gets at most one
event row. An event may be a parameter of a function in the same file when every call of that function in the
file passes a matching literal; the check follows one hop and never crosses a file. Setup on a named library
logger, such as `logging.getLogger("httpx").addFilter(...)` or `setLevel`, is not own setup.

What the check does not see, so a clean result is not a proof:

- A logger handed in without an annotation, and one bound in another file.
- An `extra=` given as a variable, and any `**fields`; the record still drops a denied field at runtime.
- A logging function imported by name, such as `from logging import warning`.
- An event passed as a keyword (`event=`, `msg=`); only the positional event is read.
- An annotated or augmented assignment to `sys.excepthook`; only a plain assignment is own setup.
- A parameter reassigned before it is logged; the check judges the literal each caller passes.
- A `@staticmethod` whose event parameter is called through `self`, which reads as `event_not_literal`, a false
  positive.
- The uvicorn command line's `--log-level` in a Dockerfile or Compose file.

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
