# logline vision

## What it does

logline makes every app and service of the fleet log the same way, so that no one can step out of line. One
call sets up the logger; from then on every line, the app's, the framework's and the libraries', is a JSON
object of one shape on stdout. Every HTTP request leaves exactly one line that says what was asked of which
route, how it ended, how long it took, for which person and from which address. A request carries one trace
across every service it touches, so the lines of one request can be found together. It records references and
never contents: ids and the trace id, not bodies, messages, prompts, tokens, cookies or paths. A fix to what
the fleet logs is made once, and every app gets it by taking the next release. logline also holds the fleet's
logging standard: where every server and app logs, how events are named and which fields a line carries, so
that one trace's lines show what happened, where the time went and what failed. Every line that closes a piece
of work is also its span: it names itself and the work that started it, so the lines of one trace alone
rebuild the exact tree of what called what and where the time went, with no tracing library in the app and no
span store. The line is the only thing an app emits about itself: counts, rates and alerts are views derived
from lines, never a second channel in the app. A trace reaches every call a process makes without the caller's
hand, so no call site can drop it. Every connection and every end leaves its line: a socket session like a
request, a crash like any error. The standard is data: one description of the record, the event names and the
fields, from which the documents' tables, the fleet's check, a host's tests and the platform's views are
derived, so none can drift from another. It is made by and for agents: every rule a machine can check is in
the checks, and the documents say what an agent cannot derive from the code.

## Who uses it today

The operator and the agents that build and run his apps; the fleet's repositories adopt it one at a time.

## Deliberate non-goals

- No log storage, shipping or search: collecting and keeping lines belongs to the platform.
- No tracing backend: no SDK, no exporter, no span store; a trace is its lines.
- No metrics client and no second emission channel.
- No framework of its own: it speaks ASGI and imports no web framework.
- No options for the record's shape and no switch that turns the request line off.
- No masking or redaction of lines, and no cap on other libraries' lines below INFO beyond the few the README
  names: `LOG_LEVEL=debug` is the operator's deliberate setting, and a further cap comes only with a case that
  needs it.
- No compatibility layers: a change that breaks its callers breaks cleanly and says so.
