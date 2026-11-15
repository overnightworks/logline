# logline vision

## What it does

logline makes every app and service of the fleet log the same way, so that no one can step out of line. One
call sets up the logger; from then on every line, the app's, the framework's and the libraries', is a JSON
object of one shape on stdout. Every HTTP request leaves exactly one line that says what was asked of which
route, how it ended, how long it took, for which person and from which address. A request carries one trace
across every service it touches, so the lines of one request can be found together.

It records references and never contents: ids and the trace id, not bodies, messages, prompts, tokens, cookies
or paths. A fix to what the fleet logs is made once, and every app gets it by taking the next release.

logline also holds the fleet's logging standard: where every server and app logs, how events are named and
which fields a line carries, so that one trace's lines show what happened, where the time went and what
failed.

It is made by and for agents: every rule a machine can check is in the checks, and the documents say what an
agent cannot derive from the code.

## Who uses it today

The operator and the agents that build and run his apps; the fleet's repositories adopt it one at a time.

## Deliberate non-goals

- No log storage, shipping or search: collecting and keeping lines belongs to the platform.
- No tracing backend: no SDK, no exporter, no span store; a trace is an id that travels.
- No framework of its own: it speaks ASGI and imports no web framework.
- No options for the record's shape and no switch that turns the request line off.
- No masking or redaction of lines, and no cap on other libraries' lines below INFO beyond the HTTP client,
  `mcp` and `sqlalchemy.dialects.postgresql` loggers at WARNING and the filters on uvicorn's loggers: `LOG_LEVEL=debug` is the operator's deliberate
  setting, and a further cap comes only with a case that needs it.
- No compatibility layers: a change that breaks its callers breaks cleanly and says so.
