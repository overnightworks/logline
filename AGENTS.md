# logline

## Commands

- `scripts/run_static_checks.sh`: every static check of CI's `checks` job, from any directory; it stops at the first failure.
- `uvx --from "git+https://github.com/overnightworks/quality@<sha>" quality check`, with the sha `.github/workflows/ci.yml` pins: the quality gate's local check.
- `uv run pytest <test module>`: a targeted run of the tests that prove a change; CI runs the suite.
- `scripts/regenerate_build_constraints.sh`: regenerates `.github/requirements-build.txt` after a change to the setuptools floor in `[build-system]` of `pyproject.toml`.
- `gh workflow run release.yml -R overnightworks/logline -f version=X.Y.Z`: an agent dispatches the release once the release pull request's review is done and CI's push run on `main` for its merge has finished green; `main` must carry X.Y.Z in `logline.__version__`.

## Rules

- `logline.__version__` is the one version. It stays a plain string literal and is raised alone, in the pull request before the release.
- A tag is never moved or re-created; a broken release gets the next patch tag. A gate refusal leaves no tag, so the release is dispatched again once its cause is gone; after a failed `tag` job, re-run the failed jobs, never all jobs.
- A new field on a line is an id, a number whose name carries its unit, or a code from a closed set; never content, a credential, a header value, a raw path or a query string.
- FastAPI, uvicorn, websockets and every third-party HTTP client are test dependencies only. logline imports no web framework or server, and imports an HTTP client only in the one module that carries the trace on outgoing requests, a third-party one only where it is installed; `.importlinter` enforces this.
- Runtime behaviour is proven by driving a real FastAPI app through the middleware under a real `uvicorn.Server`, and the non-ASGI writer `RequestLine` through a real standard-library `ThreadingHTTPServer` in a thread, and reading stdout, with only the clock and randomness faked; pure logic, such as `logline.check`, by unit tests. Branch coverage stays 100 %.
- The trace id has one owner, OpenTelemetry's context through `logline.trace`; no module keeps a second copy.
- The public modules are `logline`, `logline.check`, `logline.testing` and `logline.trace`; a name is public when a public module's `__all__` lists it, and every other module declares `__all__: list[str] = []`. A pull request that changes a public name states its **Breaking** line and the hosts' migration or "No migration".

## Owners

- An agent dispatches `release.yml` as the Commands line says.
- logline's README standard owns the record shape, field names and turn rule. The platform owns collection, retention, the format cell (reading logline's README, never restating it), Traefik and LiteLLM lines and the dashboard.

## Documents

- `README.md`: read when you change what a host calls, configures or sees.
- `docs/ARCHITECTURE.md`: read when you change which module owns what, or a boundary.
- `docs/VISION.md`: read when you judge whether a capability belongs in logline.
