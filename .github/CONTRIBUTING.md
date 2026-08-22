# Contributing

Thanks for looking. This project is alpha: the CLI surface, the MCP tool
schemas, and the query language may still change. Bug reports with a
reproduction, and notes on where the documentation misled you, are the most
useful contributions right now.

How this project writes prose — README, `CHANGES`, release notes, commit
messages, docstrings, and source comments — is set out separately in
[WRITING.md](WRITING.md). Read that before changing any of it. The
constraints every change is held to, and the map of what is where, are in
[AGENTS.md](../AGENTS.md).

## Getting set up

```console
$ uv sync --all-groups
```

`[tool.uv] python-preference = "system"` deliberately resolves the
interpreter from `PATH` (mise or your system Python matching
`.tool-versions`) instead of one of uv's own managed downloads: uv's default
`managed` preference can select a free-threaded 3.14 build, and the optional
`orjson` accelerator refuses to build on free-threaded CPython. If `uv sync`
fails while building `orjson`, check that the interpreter on `PATH` is a GIL
build.

## The gates

Format:

```console
$ uv run ruff format .
```

Lint:

```console
$ uv run ruff check . --fix --show-fixes
```

Type-check ([ty](https://github.com/astral-sh/ty), not mypy):

```console
$ uv run ty check
```

Test:

```console
$ uv run pytest
```

CI runs the check forms of the first two (`ruff format --check .`,
`ruff check .` with no `--fix`) plus `ty check --output-format github` and
`pytest -m ""` — see below for what clearing the marker expression changes.
`just watch-ruff` and `just watch-ty` (both need `entr`) re-run lint or
type-check on save.

Documentation is a gate, not a courtesy: examples in docstrings and
documentation pages are executed by `pytest`, through two different
mechanisms with no separate doctest step. Which blocks qualify, and the one
mistake that silently removes a test, are in
[WRITING.md](WRITING.md#documented-examples-that-run).

Before claiming a test or a gate works, show it failing. A gate that has
never been red is an assumption.

## Tests

The literal local loop selects tests not marked `slow`:

```console
$ uv run pytest
```

which is also `just test`. Run the exhaustive suite, including `slow`
coverage — required before calling a branch done, and what CI runs:

```console
$ just test-all
```

Run one file or node id directly, e.g.
`uv run pytest tests/test_pydantic_boundary.py`, or clear the default
selector to reach a `slow` node:

```console
$ uv run pytest \
    -m "" \
    tests/test_mcp_response_limiting.py::test_client_accepts_truncated_structured_tool_as_error
```

Run tests continuously with the default selector (`uv run ptw .` under the
hood):

```console
$ just start
```

### Test clusters by changed files

Run the default loop first, then add every resource cluster that owns a
changed surface:

| Changed files | Required lane |
| --- | --- |
| Compatibility facade, CLI helpers, discovery, readers, adapters, or query/engine code | `just test` |
| `src/agentgrep/ui/**` or TUI tests | `just test-tui` |
| `src/agentgrep/mcp/**`, `fastmcp.json`, or MCP schemas | `just test-mcp` |
| `README.md`, `docs/**`, or `src/pytest_documentation/**` | `just test-docs` |
| `fastmcp.json` or retained setup configuration | `just test-setup` |
| Packaging, lockfiles, client configuration, skills, or module boundaries | `just test-all` |

Resource markers (`mcp`, `setup`, `tui`, `documentation`) describe ownership;
`slow` describes execution cost. Prefer a module-level resource marker for a
coherent file and a function-level `slow` marker for mounted apps, fresh MCP
clients, subprocesses, Sphinx builds, races, or exhaustive matrices. New
unmarked tests run by default. Do not add new tests to a catch-all module;
give each critical contract a focused module instead.

### Testing guidelines

- Write tests as standalone functions, not `class TestFoo:` groupings, and do
  not reintroduce `unittest.TestCase`. For stateful engine/driver behavior,
  use fixtures, typed case helpers, and parametrized tables when a flat
  function would obscure the state machine.
- Use fixtures from `conftest.py` over `monkeypatch` and `MagicMock` when one
  is available. Document in the test docstring why a standard fixture wasn't
  used for an exceptional case.
- Prefer `tmp_path` over `tempfile`, and `monkeypatch` over `unittest.mock`
  or direct attribute assignment — auto-revert matters.
- Use `syrupy` snapshots when the expected output is large or fragile to
  inline.
- `asyncio_mode = "auto"` is set: an `async def test_*` is awaited without an
  explicit `@pytest.mark.asyncio` decorator.

Add a test only when it protects a critical user-visible or architectural
contract not already exercised elsewhere; prefer the cheapest stable layer
that can prove the behavior. See
[tests/AGENTS.md](../tests/AGENTS.md) for the test suite's own latency
budget, cache-safety rules, and synchronize-on-signal-not-clock discipline.

## Coding standards

**Imports.** Namespace-import the standard library — `import enum`, not
`from enum import Enum` — except `dataclasses`, which may use
`from dataclasses import dataclass, field`. Third-party packages may use
`from X import Y`. Use `import typing as t` and reference types through the
namespace. Every file starts with `from __future__ import annotations`.

Function-local imports are acceptable when the target module is heavy (a C
extension, Pydantic model registration, a large parser) and the call site is
reached only by a specific subcommand — this keeps `agentgrep --help`'s
import graph off the common path. Guard the corresponding type-only import
with `if t.TYPE_CHECKING:` so `ty` resolves the annotation without triggering
the runtime import. The current ruff configuration does not flag
function-local imports (`PLC` is not in `extend-select`); if it is ever
enabled, add the affected paths to `per-file-ignores` instead of reverting
the pattern.

**Synchronization.** Coordinate by publishing and subscribing — a callback,
queue, `Event`, future, or progress sink — never by waiting out a duration. A
non-zero `time.sleep` is a defect to refactor, not a constant to tune.
`time.sleep(0)` and `await asyncio.sleep(0)` are not sleeps; they yield the
scheduler, which is how a long scan lets the UI thread render. Blocking on a
signal with a generous timeout is a deadlock failsafe, provided expiry is
treated as a failure. When the event you need is not published yet, publish
it.

**Logging.** Guidance for new code; existing code may not yet conform.

- `logging.getLogger(__name__)` per module; a `NullHandler` in library
  `__init__.py` files; never configure handlers, levels, or formatters in
  library code.
- Pass structured context via `extra` with an `agentgrep_` prefix
  (`agentgrep_source`, `agentgrep_query`, `agentgrep_command`) — `snake_case`,
  stable scalars, no ad-hoc objects. Treat established keys as
  compatibility-sensitive; downstream dashboards may depend on them.
- Heavy keys (raw matches, captured output) are DEBUG-only; add a companion
  `*_len` field or truncate hard.
- `logger.debug("msg %s", val)`, never an f-string: interpolation is skipped
  when the level is filtered, and an f-string defeats aggregator
  message-template grouping. Guard an expensive `val` with
  `if logger.isEnabledFor(logging.DEBUG)`.
- Increment `stacklevel` for each wrapper layer so the reported filename and
  line point at the real caller.
- Lowercase, past tense, no trailing punctuation: `"search completed"`, not
  `"Search Completed."`. Keep the message short; put detail in `extra`.
- `logger.exception()` only inside an `except` block you are not re-raising
  from. Use `logger.error(..., exc_info=True)` for a traceback outside an
  `except` block. Never `logger.exception()` followed by `raise` — that
  duplicates the traceback.
- Assert on `caplog.records` attributes
  (`[r for r in caplog.records if hasattr(r, "agentgrep_source")]`), not on
  `caplog.text` substrings — `caplog.record_tuples` cannot see `extra`
  fields at all.
- Avoid: f-strings/`.format()` in log calls, unguarded logging in hot loops,
  catch-log-reraise with no new context, `print()` for diagnostics, logging a
  secret env var's value (log the key name only), and required `extra` keys
  with no safe default.

Log levels: `DEBUG` for internal mechanics and backend I/O (a probe, a
subprocess command and its stdout, a SQLite query); `INFO` for user-visible
operations (search started, MCP server bound); `WARNING` for recoverable
issues and deprecations (an optional backend missing, a deprecated flag);
`ERROR` for failures that stop an operation (a backend probe failed, an
invalid query).

## Documentation

Build the site:

```console
$ just build-docs
```

which builds `dirhtml` into `docs/_build/`. Mermaid diagrams render through
Puppeteer; a from-scratch environment needs the Node toolchain once before
the first build:

```console
$ pnpm -C docs install --frozen-lockfile
```

Serve it locally with auto-reload:

```console
$ just start-docs
```

`just design-docs` instead watches and rebuilds while you edit
`docs/_static` CSS/JS.

`just build-docs` does not run Sphinx's own doctest builder. That is a
separate step, also collected as one `pytest` item from the `doctest` recipe
in `docs/justfile`, so it runs under `just test-docs` / `just test-all` as
well as standalone:

```console
$ just -f docs/justfile doctest
```

Writing conventions for documentation pages — voice, MyST roles, anchor
naming, which fences are executable — are in
[WRITING.md](WRITING.md#who-you-are-writing-for) and
[WRITING.md](WRITING.md#markdown). `just build-docs` catches a broken
cross-reference; the executable-documentation suite does not, so build the
docs before committing a page that adds a `{ref}`.

## Profiling and benchmarking

Use `scripts/profile_engine.py` for local engine-profile evidence and
`scripts/benchmark.py` for timed cross-commit sweeps. Both emit
privacy-safe, sanitized artifacts (counts, span names, durations, coarse
subprocess metadata) that must never carry prompt text, raw argv, or local
absolute paths — CI runners have no representative agent-history store, so
these local artifacts are the real evidence for a bottleneck claim. The
`profile` and `benchmark` skills (`.agents/skills/profile.md`,
`.agents/skills/benchmark.md`) carry the full component table, selector
names, and command forms; reach for those rather than reconstructing a
profiler invocation from memory.

## Releasing

Never create tags. Never push tags. The owner handles tagging and tag
pushes, because pushing a `v<version>` tag triggers the `release` job in
`.github/workflows/tests.yml`. See
[Release commits](WRITING.md#release-commits).

That job builds with `uv build` and publishes to PyPI through trusted
publishing (OIDC; no stored token), with build attestations attached.

## Pull requests

One subject per pull request. Unrelated cleanup found along the way belongs
in its own commit, and usually in its own pull request.

Discuss a substantial change via an issue before making it.

Commit format is in [WRITING.md](WRITING.md#commits).

## Decorum

- Participants will be tolerant of opposing views.
- Participants must ensure that their language and actions are free of
  personal attacks and disparaging personal remarks.
- When interpreting the words and actions of others, participants should
  always assume good intentions.
- Behaviour which can be reasonably considered harassment will not be
  tolerated.

Based on
[Ruby's Community Conduct Guideline](https://www.ruby-lang.org/en/conduct/).

## Security

Please do not open a public issue for a vulnerability. This repository has
no `SECURITY.md`; use GitHub's private vulnerability reporting instead — the
repository's Security tab, "Report a vulnerability".
