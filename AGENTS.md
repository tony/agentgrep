# AGENTS.md

This file provides guidance to AI agents (including Claude Code, Cursor, and other LLM-powered tools) when working with code in this repository.

## CRITICAL REQUIREMENTS

### Test Success
- ALL tests MUST pass for code to be considered complete and working
- Never describe code as "working as expected" if there are ANY failing tests
- Even if specific feature tests pass, failing tests elsewhere indicate broken functionality
- Changes that break existing tests must be fixed before considering implementation complete
- A successful implementation must pass linting, type checking, AND all existing tests

## Project Overview

agentgrep is a read-only search tool for local AI agent prompts and history across Codex, Claude Code, and Cursor. It ships both a CLI (`agentgrep`) and an MCP (Model Context Protocol) server (`agentgrep-mcp`), so the same search surface is reachable from a terminal or from an AI agent.

Key features:
- Cross-tool search over Codex, Claude Code, and Cursor prompt/history stores
- CLI entry point with a Textual TUI for interactive browsing
- MCP server entry point exposing the same search surface as MCP tools
- Dependency-light request, result, and event types, with Pydantic adapters
  for MCP schemas, docs, and other typed boundaries where useful
- Full type safety (ty, strict warning-as-error)

### Platform Support

agentgrep does not currently support native Windows. Windows Subsystem for
Linux (WSL) is supported. Users who want native Windows support can register
their interest in the issue tracker.

## Engineering Policies

### Python First

Python is the default implementation language, public API surface, and user
experience for this project. Start with clear, typed Python before reaching for
native code. Native implementation is appropriate only for measured hot paths,
control/latency-sensitive internals, or platform interfaces that Python cannot
reasonably handle on its own.

### Native Boundary Policy

Default to no native code. Native code must be justified by measurement of a
user-visible path against a named baseline, and it must not become the source
of public behavior by accident.

ADR 0003 is the canonical policy for native boundary shapes:

- Accelerator - a drop-in for a public Python callable. Removing the native
  build changes nothing observable except speed. ADR 0002 owns the
  compatibility rules.
- Engine - in-process native code over a normalized plan, batch, buffer, or
  scoped state. ADR 0003 owns the boundary, lifecycle, and test obligations.
- Worker - an independent process, binary, or long-lived native thread behind
  a versioned message-passing protocol. ADR 0003 owns the classification; a
  worker protocol needs its own follow-up ADR.

Do not duplicate the accelerator import pattern, CI matrix, or native change
checklists here. Keep those details in ADR 0002 and ADR 0003 so agents and
reviewers have one source of truth.

### Pure Python / Rust Accelerator Compatibility

The pure Python implementation is the semantic source of truth. Rust may make
agentgrep faster, but it must not make agentgrep less Pythonic, less portable,
less tested, or less predictable.

Use ADR 0002 for the full accelerator compatibility policy: Python-first public
behavior, optional Rust imports, shared Python/Rust behavioral tests, duck
typing preservation, documentation/type-hint alignment, CI expectations, and
`unsafe` review requirements.

## Development Environment

This project uses:
- Python 3.14+
- [uv](https://github.com/astral-sh/uv) for dependency management
- [ruff](https://github.com/astral-sh/ruff) for linting and formatting
- [ty](https://github.com/astral-sh/ty) for type checking
- [pytest](https://docs.pytest.org/) for testing
  - [pytest-watcher](https://github.com/olzhasar/pytest-watcher) for continuous testing
  - [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio) for the MCP server tests
  - [syrupy](https://github.com/syrupy-project/syrupy) for snapshot assertions

## Common Commands

### Setting Up Environment

```bash
# Install dependencies
uv pip install --editable .
uv pip sync

# Install with development dependencies
uv pip install --editable . -G dev
```

### Running Tests

```bash
# Run all tests
just test
# or directly with pytest
uv run pytest

# Run a single test file
uv run pytest tests/test_agentgrep.py

# Run a specific test
uv run pytest tests/test_agentgrep.py::test_json_output_falls_back_without_pydantic

# Run tests with test watcher
just start
# or
uv run ptw .

# Run tests with doctests
uv run ptw . --now --doctest-modules
```

### Linting and Type Checking

```bash
# Run ruff for linting
just ruff
# or directly
uv run ruff check .

# Format code with ruff
just ruff-format
# or directly
uv run ruff format .

# Run ruff linting with auto-fixes
uv run ruff check . --fix --show-fixes

# Run ty for type checking
just ty
# or directly
uv run ty check

# Watch mode for linting (using entr)
just watch-ruff
# Watch mode for type checking (native ty --watch)
just watch-ty
```

### Development Workflow

Follow this workflow for code changes:

1. **Format First**: `uv run ruff format .`
2. **Run Tests**: `uv run pytest`
3. **Run Linting**: `uv run ruff check . --fix --show-fixes`
4. **Check Types**: `uv run ty check`
5. **Verify Tests Again**: `uv run pytest`

### Documentation

```bash
# Build documentation
just build-docs

# Start documentation server with auto-reload
just start-docs

# Update documentation CSS/JS
just design-docs
```

### Focused Local Checks

Use focused checks while iterating, then run the completion gate before calling
the branch done.

Recommended focused checks:

- Docs-only AGENTS/ADR edits: `git diff --check` and `just build-docs`.
- Python implementation edits: `uv run ruff check .`, `uv run ty check`, and
  the touched pytest files or node ids.
- CLI/MCP/query surface edits: add the relevant CLI, MCP, query, event-stream,
  and documentation tests that prove the public behavior.
- Formatting-only edits: `uv run ruff format .` and `git diff --check`.

Focused checks are local feedback. They are not a substitute for the completion
gate when reporting that a branch is ready.

### Required Completion Gate

Before describing code as complete, working, PR-ready, merge-ready, or
release-ready, run the full repository gate:

```console
$ rm -rf docs/_build; uv run ruff check . --fix --show-fixes; uv run ruff format .; uv run ty check; uv run py.test --reruns 0 -vvv; just build-docs;
```

Do not claim completion until that command exits successfully. If it fails, fix
the failure and rerun the full command rather than relying on partial checks.
If an intermediate commit uses only focused checks, state that clearly in the
commit or PR context and run the full gate before the branch is presented as
done.

### Profiling and Benchmarking

Use `scripts/profile_engine.py` for local engine-profile evidence. It emits
privacy-safe timings with counts, span names, durations, and coarse subprocess
metadata. It must not emit prompt text, raw argv, or local absolute paths.

Supported profiler components:

| Component | Use |
| --- | --- |
| `search-prompts` | Prompt-scope search engine timing |
| `search-conversations` | Conversation-scope search engine timing |
| `grep-prompts` | Prompt-scope grep-shaped engine timing |
| `grep-conversations` | Conversation-scope grep-shaped engine timing |
| `find-prompts` | Prompt-source enumeration timing |
| `all` | Run every profiler component above |

Run one profiler component and save a machine-readable artifact:

```console
$ uv run python scripts/profile_engine.py grep-prompts --agent all --max-count 500 --json tmux > .tmp/profile-grep-prompts.json
```

Run the Cursor IDE SQLite path directly:

```console
$ uv run python scripts/profile_engine.py search-prompts \
    --agent cursor-ide \
    --limit 500 \
    --format json \
    agentgrep-cursor-db-no-match > .tmp/profile-cursor-ide.json
```

Run the full profiler matrix and save a machine-readable artifact:

```console
$ uv run python scripts/profile_engine.py all --agent all --limit 500 --json tmux > .tmp/profile-all.json
```

Profiler output defaults to a Rich terminal summary. Use `--json` for one
sanitized payload, `--ndjson` for one child profile run per line, and
`--top-spans N` to control the terminal summary. The explicit
`--format json`, `--format ndjson`, and `--format rich` forms remain available
for templated invocations.

Profiler artifacts include `schema_version` and `artifact_kind`. Use those
fields when a local profile file needs to be distinguished from benchmark rows
or future fixture-only CI artifacts. Engine profiles include coarse phase spans
and source-level spans such as `search.discover.group`,
`search.plan.decision`, `search.plan.strategy_group`,
`search.plan.prefilter_root`,
`search.plan.direct_source`, `search.collect.source`, optional
`search.collect.scheduler`, optional `search.collect.source_scan_cache`,
and `find.filter.source`; those spans carry
agent/store/adapter/count metadata without prompt text or local paths.
`search.collect.scheduler` is the driver
summary for source-level scheduling and reports worker, submitted, completed,
skipped, cancellation-requested, batch, queued-batch, queue-wait, and emitted
counts. `search.collect.source_scan_cache` reports cache-hit lookups when a
runtime source-scan cache is active.

Use `scripts/benchmark.py` for timed benchmark sweeps. The profiler-oriented
benchmark entries are named `profile-engine-*`; each committed benchmark name
and description must disclose `--limit N` or `--max-count N` when a cap is
present. Use `--commands profile-engine` for the all-agent profiler
benchmark group, or pass an exact `profile-engine-*` key for one profiler
benchmark.
Use `--commands profile-engine-cursor-ide` for the Cursor IDE SQLite benchmark
set without expanding the all-agent profiler group.

Run one profiler benchmark:

```console
$ uv run scripts/benchmark.py run \
    --target HEAD \
    --commands profile-engine-grep-all-prompts-max-count-500 \
    --format json \
    --output .tmp/benchmark-grep-prompts.json \
    --allow-dirty
```

Run every profiler benchmark:

```console
$ uv run scripts/benchmark.py run \
    --target HEAD \
    --commands profile-engine \
    --format json \
    --output .tmp/benchmark-profile-engine.json \
    --allow-dirty
```

Run the Cursor IDE SQLite profiler benchmark set against two branch tips:

```console
$ uv run scripts/benchmark.py run \
    --commits streamline-02,streamline-03 \
    --commands profile-engine-cursor-ide \
    --runs 25 \
    --show-percentiles min,avg,max,p90,p95,p99 \
    --format json \
    --output .tmp/benchmark-cursor-ide-profile-engine.json \
    --allow-dirty \
    --no-progress
```

Benchmark `json` and `ndjson` artifacts include `dry_run`,
`profile_payload`, `profile_capture_error`, `schema_version`, and
`artifact_kind`. `command_string` is sanitized with `{repo}`, `{venv}`,
`{home}`, and `{query}` placeholders. For `profile-engine-*` rows,
`profile_payload` is a separate post-timing profile capture; timing
conclusions must come from `samples`. Use `--format rich --top-spans N` to
render nested `profile_payload` spans in the terminal, or `--top-spans 0` to
hide that table.

Analyze saved benchmark artifacts before writing bottleneck summaries:

```console
$ uv run scripts/benchmark.py analyze \
    .tmp/benchmark-profile-engine.json \
    --format rich \
    --top-spans 20 \
    --top-groups 10
```

Use `--format json` or `--format ndjson` for machine-readable analysis
artifacts. Analyzer output uses `agentgrep.benchmark.analysis` metadata and
summarizes command timings, slow profile spans, profile span groups, and
warnings without local paths, raw argv, or prompt text.

Local profiles are the source of real bottleneck evidence because CI runners do
not have representative agent-history stores. If CI artifact upload is needed,
keep it scoped to a separate issue and use sanitized fixture-only payloads.

### OpenTelemetry and LGTM

OpenTelemetry instrumentation lives behind `agentgrep._telemetry`. Do not
import OpenTelemetry SDK/exporter modules from normal application paths;
`agentgrep._telemetry_otel` is the lazy optional backend. Packaged users must
keep working when OTel dependencies, LGTM, Docker, or OTLP endpoints are
absent.

Use `AGENTGREP_OTEL` as the single project telemetry switch. Do not add a
second enable variable. Local checkouts default to passive local telemetry;
packaged installs stay quiet unless explicitly enabled. Telemetry setup,
export, and shutdown failures must never change CLI, TUI, MCP, or test
correctness.

`service.version` is the package version only. Do not put debug attempts,
dirty candidates, pytest retries, or agent-loop identifiers in
`service.version`. Use separate attributes such as
`agentgrep.debug.session_id`, `agentgrep.debug.candidate_id`,
`agentgrep.debug.attempt`, and `agentgrep.pytest.run_id`.

Do not create empty root spans or orphaned low-level spans. Root spans must be
app-level operations such as `agentgrep.cli.invocation`,
`agentgrep.cli.interactive_session`, `agentgrep.tui.session`,
`agentgrep.mcp.request`, `agentgrep.mcp.tool`, `agentgrep.benchmark.run`,
`agentgrep.profile_engine.run`, `agentgrep.pytest.session`,
`agentgrep.pytest.test`, or `agentgrep.otel.smoke`. Child spans should
represent logical work, not every keypress, render frame, event-loop callback,
or internal dispatch.

SQLite telemetry must cover `sqlite3.Connection` shortcut methods through
`agentgrep._telemetry.sqlite_connection_factory()`. Do not rely on
`SQLite3Instrumentor` alone for SQLite spans; it does not cover the connection
shortcut path agentgrep uses for source parsing. SQL spans must be children of
an existing app trace and must not include bound parameter values, prompt text,
file contents, or local database paths. SQLite and CPU-impacting work metrics
must come from normal app, profiler, benchmark, CLI, TUI, MCP, and pytest paths,
not only from synthetic smoke scripts.

Logs exported through OTel must be trace-linked. Do not export unparented
logs, raw prompts, raw MCP arguments, raw argv, environment values, file
contents, secrets, or full local paths. Use redacted shape metadata and stable
low-cardinality attributes.

When `AGENTGREP_OTEL` is enabled and `AGENTGREP_DEBUG_SESSION_ID` is present,
traces, logs, metrics, and profiles must all carry enough run identity to
prove coverage in Grafana. Do not add another agentgrep-specific OTel feature
flag to hide metrics, traces, logs, or profiles. If metric cardinality later
becomes a measured project risk, handle that in a follow-up rather than
preserving blindspots in the instrumentation branch.

Any new subprocess, exporter, auto-instrumentation hook, benchmark command, or
profiling loop must update `docs/dev/otel-cost-model.md` with its runtime cost
and signal impact. This includes benchmark warmups, sample counts, extra
profile-payload captures, Docker/LGTM helper commands, pytest subprocesses,
and OTLP/Pyroscope flush costs.

Default pytest must be deterministic and offline. Use in-memory telemetry for
unit assertions. Explicitly instrumented pytest runs use pytest hooks so every
collected item, including custom documentation items and direct Textual
`run_test()` cases, gets an `agentgrep.pytest.test` root. Live LGTM checks are
opt-in through `just otel-acceptance`; they must prove traces, metrics, logs,
and profiles against the real stack without making ordinary tests depend on
Docker or network ports.

## Code Architecture

agentgrep is no longer a single-module surface. Treat this section as an
operational source map, not as the architectural source of truth. Durable
behavior belongs in ADRs and focused public-surface docs.

```
src/agentgrep/
  __init__.py       # public compatibility facade, record dataclasses, legacy helpers
  __main__.py       # `python -m agentgrep` entry point
  cli/              # argparse surface and text/JSON/NDJSON renderers
  query/            # field registry, parser, AST, compiler, date helpers
  events.py         # typed SearchEvent / FindEvent stream types
  _engine/          # planning, matching, scanning, scheduling, runtime, profiling
  mcp/              # FastMCP server, models, middleware, resources, prompts, tools
  ui/               # Textual application
  store_catalog.py  # store discovery/catalog helpers
  stores.py         # typed store descriptors and availability metadata
  ranking.py        # ranking helpers shared by engine/frontend surfaces
```

### Core Modules

1. **Compatibility facade** (`src/agentgrep/__init__.py`)
   - Public compatibility exports and legacy helper implementations while the
     split modules continue to settle
   - `SearchRecord` / `FindRecord` dataclasses and serialization-compatible
     payload types
   - Backward-compatible entry points for CLI, TUI, and JSON output helpers

2. **`python -m` entry** (`src/agentgrep/__main__.py`)
   - Thin wrapper so `python -m agentgrep` works alongside the console script

3. **CLI surface** (`src/agentgrep/cli/`)
   - Argument parsing and output rendering for `agentgrep`
   - JSON result payload construction, NDJSON/event rendering, and
     pydantic-aware serialization with a pydantic-free fallback path

4. **Query language** (`src/agentgrep/query/`)
   - Search field registry, AST, parser, compiler, and date matching helpers
   - Keep query semantics frontend-neutral; CLI and MCP should consume the
     same compiled request/plan vocabulary

5. **Execution engine** (`src/agentgrep/_engine/`)
   - Query planning, matching, source scanning, scheduling, runtime orchestration,
     and profiling
   - ADR 0004 owns the planning/execution/result-stream architecture

6. **MCP server** (`src/agentgrep/mcp/`)
   - FastMCP assembly, middleware, input/output models, resources, prompts, and
     tools
   - Pydantic models adapt public request/result types for MCP schemas; they
     do not own search semantics

7. **TUI** (`src/agentgrep/ui/`)
   - Textual application for interactive browsing of normalized records
   - Keep blocking discovery, parsing, and ranking work behind the execution
     engine rather than on the UI event loop

### Non-blocking TUI rules

The Textual message pump is single-threaded: any callable it invokes that runs
past a frame budget — or never returns — freezes keystrokes, the spinner,
resize, and cancel at once. ADR 0011 (NB-1..NB-10) is the contract; the
`textual-non-blocking-pump` skill is the working method. On every `ui/` change:

- **Enumerate pump entrypoints, do not prefix-guess.** Textual runs your code on
  the pump through `on_*`/`_on_*` and **any `@on(...)`-decorated handler**, inline
  reactive `watch_*`/`validate_*`/`compute_*`, `render`/`__rich__`/`get_content_*`,
  `action_*`, and **the callables passed to `set_timer`/`set_interval`/`call_later`/
  `call_from_thread`/`subscribe`**. Decorate any new pump entrypoint `@pump_only`;
  decorate every `run_worker` target `@offload` (`thread=True`, `exclusive=True`
  except `group="history"`, stable `group=`).
- **No blocking work reachable from a pump callable — even one helper hop down.**
  No file open, subprocess, sqlite3, network, filesystem walk, lock/queue wait,
  `concurrent.futures` `.result()`, `json.load(s)`/`dump(s)`, `.read()`, or
  **unbounded CPU** (full-result casefold/sort/regex, `Syntax(...).highlight` on a
  full body). Route bulk UI updates through `stream_apply`; route large/uncached
  detail builds through an `@offload` worker. Never satisfy the guard by
  aliasing/`from`-import — move the call off the pump.
- **The static guard cannot reach 100%.** "Blocks the pump" is a semantic
  (Rice-undecidable) property; `tests/test_tui_non_blocking.py` follows
  same-class helper calls, seeds `@on`/scheduler/`call_from_thread`/`subscribe`
  callees, and resolves import aliases — but it still cannot see cross-module or
  dynamic dispatch, a `lambda`/`partial` scheduler target, or CPU spin. Apply the
  skill's review rules by hand, and exercise the change once under
  `AGENTGREP_TUI_WATCHDOG=1` against a large real store before calling a path
  non-blocking.

### Backend availability

agentgrep is opportunistic about its dependencies. `pydantic`, `textual`, and
`fastmcp` are declared, but the CLI JSON path must keep its pydantic-free
fallback so basic search output remains available when Pydantic cannot be
imported. Treat Pydantic as a schema, validation, and adapter layer at explicit
boundaries; do not make Pydantic-only model behavior the semantic source of
truth for CLI/MCP/search behavior. When adding code that imports an optional
dependency, keep the fallback path intact and covered by a test (see
`test_json_output_falls_back_without_pydantic` for the pattern).

## Testing Strategy

agentgrep uses pytest with `--doctest-modules` enabled by default (`testpaths = ["src/agentgrep", "tests"]`). Tests live under `tests/` and are split by entry point: `test_agentgrep.py` for the library/CLI/TUI and `test_agentgrep_mcp.py` for the MCP server.

### Testing Guidelines

1. **Use functional tests by default**: Write tests as standalone functions,
   not classes. Avoid `class TestFoo:` groupings for namespacing, and do not
   reintroduce `unittest.TestCase`. For stateful engine/driver behavior, use
   fixtures, typed case helpers, and parametrized tables when a flat function
   would obscure the state machine or behavior matrix.

2. **Use existing fixtures over mocks**
   - Use fixtures from `conftest.py` instead of `monkeypatch` and `MagicMock` when available
   - Document in test docstrings why standard fixtures weren't used for exceptional cases

3. **Preferred pytest patterns**
   - Use `tmp_path` (pathlib.Path) fixture over Python's `tempfile`
   - Use `monkeypatch` fixture over `unittest.mock` (and over direct attribute assignment — auto-revert matters)
   - Use `syrupy` snapshots when the expected output is large or fragile to inline

4. **Async tests use auto mode**
   - `asyncio_mode = "auto"` is set, so `async def test_*` functions are awaited without an explicit `@pytest.mark.asyncio` decorator

5. **Running tests continuously**
   - Use pytest-watcher during development: `uv run ptw .`
   - For doctests: `uv run ptw . --now --doctest-modules`

### Example Fixture Usage

```python
def test_json_output_falls_back_without_pydantic(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON output works when pydantic isn't importable."""
    agentgrep = load_agentgrep_module()
    # ... build a SearchRecord ...
    monkeypatch.setattr(agentgrep.importlib, "import_module", fake_import_module)
    # ... assert result payload shape ...
```

## Coding Standards

Key highlights:

### Imports

- **Use namespace imports for standard library modules**: `import enum` instead of `from enum import Enum`
  - **Exception**: `dataclasses` module may use `from dataclasses import dataclass, field` for cleaner decorator syntax
  - This rule applies to Python standard library only; third-party packages may use `from X import Y`
- **For typing**, use `import typing as t` and access via namespace: `t.NamedTuple`, etc.
- **Use `from __future__ import annotations`** at the top of all Python files
- **Lazy imports for CLI cold-start**: function-local imports are
  acceptable when the target module is heavy (C extensions like
  `rapidfuzz`, pydantic model registration, large AST parsing) and
  the call site is only reached by a specific subcommand. This keeps
  `agentgrep --help` under 250 ms. Use `if t.TYPE_CHECKING:` at the
  top for type annotations referencing the lazy-imported module so
  ty resolves the types without triggering the runtime import. The
  current ruff config does not flag function-local imports (`PLC` is
  not in `select`); if it is ever enabled, add the relevant paths to
  `per-file-ignores`. Pattern follows CPython's own
  `Lib/importlib/__init__.py`. Bounded introspection subcommands, such
  as future `query fields` or `query explain` commands, may import the
  query registry and planner because their purpose is introspection; keep
  the root help path cold.

### Docstrings

Follow NumPy docstring style for all functions and methods:

```python
"""Short description of the function or class.

Detailed description using reStructuredText format.

Parameters
----------
param1 : type
    Description of param1
param2 : type
    Description of param2

Returns
-------
type
    Description of return value
"""
```

### Doctests

agentgrep is part library, part CLI/TUI, part MCP server. Most of the
surface area reads the user's home directory, parses Codex/Claude/Cursor
stores, or talks to an MCP client — so a blanket doctest mandate doesn't
fit the shape of the code. Scope doctests to functions where they
actually work offline.

**Where doctests SHOULD be used:**
- Pure helper functions (parsers, formatters, serializers, redaction
  logic, small utilities) that can run with no external state.
- Examples in module-level docstrings that illustrate a concept without
  hitting the filesystem, a subprocess, or the network.

**Where doctests are exempt:**
- Any function that reads the user's home directory, opens a Codex /
  Claude / Cursor store, spawns `ripgrep`, opens a SQLite database, or
  starts the Textual TUI. Use a unit test with fixtures instead.
- MCP tool implementations — they require a FastMCP context. Test via
  `tests/test_agentgrep_mcp.py`.

**CRITICAL RULES for doctests that exist:**
- They MUST actually execute — never comment out function calls or
  similar.
- They MUST NOT be converted to `.. code-block::` as a workaround
  (code-blocks don't run).
- `# doctest: +SKIP` is discouraged. If a function can't run offline,
  write a unit test instead of a skipped doctest — a skipped test is
  just noise.

**When output varies, use ellipsis:**
```python
>>> record.path  # doctest: +ELLIPSIS
PosixPath('.../codex/...')
```

### Logging Standards

These rules guide future logging changes; existing code may not yet conform.

#### Logger setup

- Use `logging.getLogger(__name__)` in every module
- Add `NullHandler` in library `__init__.py` files
- Never configure handlers, levels, or formatters in library code — that's the application's job

#### Structured context via `extra`

Pass structured data on every log call where useful for filtering, searching, or test assertions. Use the `agentgrep_*` prefix for project-specific keys (e.g., `agentgrep_source` for the backend tag — `codex` / `claude` / `cursor`, `agentgrep_query` for the search query, `agentgrep_command` for the subcommand). Prefer stable scalars; avoid ad-hoc objects.

Treat established keys as compatibility-sensitive — downstream users may build dashboards and alerts on them. Change deliberately.

#### Key naming rules

- `snake_case`, not dotted; `agentgrep_` prefix
- Prefer stable scalars; avoid ad-hoc objects
- Heavy keys (raw matches, captured output) are DEBUG-only; consider companion `*_len` fields or hard truncation

#### Lazy formatting

`logger.debug("msg %s", val)` not f-strings. Two rationales:
- Deferred string interpolation: skipped entirely when level is filtered
- Aggregator message template grouping: `"Running %s"` is one signature grouped ×10,000; f-strings make each line unique

When computing `val` itself is expensive, guard with `if logger.isEnabledFor(logging.DEBUG)`.

#### stacklevel for wrappers

Increment for each wrapper layer so `%(filename)s:%(lineno)d` and OTel `code.filepath` point to the real caller. Verify whenever call depth changes.

#### Log levels

| Level | Use for | Examples |
|-------|---------|----------|
| `DEBUG` | Internal mechanics, backend I/O | Backend probe, subprocess command + stdout, SQLite query |
| `INFO` | User-visible operations | Search started, MCP server bound |
| `WARNING` | Recoverable issues, deprecation | Optional backend missing, deprecated flag |
| `ERROR` | Failures that stop an operation | Backend probe failed, invalid query |

#### Message style

- Lowercase, past tense for events: `"search completed"`, `"backend probe failed"`
- No trailing punctuation
- Keep messages short; put details in `extra`, not the message string

#### Exception logging

- Use `logger.exception()` only inside `except` blocks when you are **not** re-raising
- Use `logger.error(..., exc_info=True)` when you need the traceback outside an `except` block
- Avoid `logger.exception()` followed by `raise` — this duplicates the traceback. Either add context via `extra` that would otherwise be lost, or let the exception propagate

#### Testing logs

Assert on `caplog.records` attributes, not string matching on `caplog.text`:
- Scope capture: `caplog.at_level(logging.DEBUG, logger="agentgrep")`
- Filter records rather than index by position: `[r for r in caplog.records if hasattr(r, "agentgrep_source")]`
- Assert on schema: `record.agentgrep_source == "codex"` not `"codex" in caplog.text`
- `caplog.record_tuples` cannot access extra fields — always use `caplog.records`

#### Avoid

- f-strings/`.format()` in log calls
- Unguarded logging in hot loops (guard with `isEnabledFor()`)
- Catch-log-reraise without adding new context
- `print()` for diagnostics
- Logging secret env var values (log key names only)
- Non-scalar ad-hoc objects in `extra`
- Requiring custom `extra` fields in format strings without safe defaults (missing keys raise `KeyError`)

### Git Commit Standards

Format commit messages as:
```
Scope(type[detail]): concise description

why: Explanation of necessity or impact.

what:
- Specific technical changes made
- Focused on a single topic
```

Keep the subject ≤50 chars (excluding any trailing `(#NN)` PR ref); wrap
body lines at ≤72 chars. Separate the `why:` and `what:` blocks with a
blank line.

Common commit types:
- **feat**: New features or enhancements
- **fix**: Bug fixes
- **refactor**: Code restructuring without functional change
- **docs**: Documentation updates
- **chore**: Maintenance (dependencies, tooling, config)
- **test**: Test-related updates
- **style**: Code style and formatting
- **py(deps)**: Dependencies
- **py(deps[dev])**: Dev Dependencies
- **ai(rules[AGENTS])**: AI rule updates
- **ai(claude[rules])**: Claude Code rules (CLAUDE.md)
- **ai(claude[command])**: Claude Code command changes

Example:
```
agentgrep(refactor[typecheck]): Satisfy ty diagnostics

why: ty reports a few stricter diagnostics around TypedDict payloads, dynamic class bases, and monkeypatched imports. Making those cases explicit keeps the runtime behavior unchanged while letting the new ty gate run without suppressing broad categories of checks.

what:
- Cast JSON TypedDict payloads directly in the pydantic-free fallback instead of rebuilding them through dict().
- Mark the dynamic Textual App base with the targeted ty unsupported-base suppression.
- Use pytest monkeypatch for the importlib fallback test instead of assigning over the imported module function directly.
```
#### Release commits

Never create tags. Never push tags. The user handles tagging and tag
pushes (tags trigger the CI publish workflow).

Release commit subjects are plain and short: `Tag v<version>`. Put
the detailed why/what in the commit body. Don't use the
`Scope(type[detail]):` format for releases — don't bury the lede.

For multi-line commits, use heredoc to preserve formatting:
```bash
git commit -m "$(cat <<'EOF'
feat(Component[method]) add feature description

why: Explanation of the change.

what:
- First change
- Second change
EOF
)"
```

## Documentation Standards

### Sphinx Cross-Reference Roles for MCP Tools

agentgrep's docs use `sphinx-autodoc-fastmcp` (see `docs/conf.py`,
`fastmcp_section_badge_pages`). The same role family applies:

- `{tool}` — code chip + full safety badge (text + icon). Use in **headers, bulleted lists, and tables** where the badge provides scannable context.
- `{tooliconl}` — code chip + small colored square icon (left). Use in **inline paragraph text** where the full badge is too visually heavy.
- `{toolref}` — code chip only, no badge. Use for **dense inline sequences** or explanatory text where the safety tier is already established.
- `{tooliconil}` / `{tooliconir}` — bare emoji inside code chip. Use for **compact lists and scan-heavy surfaces**.

### Code Blocks in Documentation

When writing documentation (README, CHANGES, docs/), follow these rules for code blocks:

**One command per code block.** This makes commands individually copyable. For sequential commands, either use separate code blocks or chain them with `&&` or `;` and `\` continuations (keeping it one logical command).

**Put explanations outside the code block**, not as comments inside.

Good:

Run the tests:

```console
$ uv run pytest
```

Run with coverage:

```console
$ uv run pytest --cov
```

Bad:

```console
# Run the tests
$ uv run pytest

# Run with coverage
$ uv run pytest --cov
```

### Shell Command Formatting

These rules apply to shell commands in documentation (README, CHANGES, docs/), **not** to Python doctests.

**Use `console` language tag with `$ ` prefix.** This distinguishes interactive commands from scripts and enables prompt-aware copy in many terminals.

Good:

```console
$ uv run pytest
```

Bad:

```bash
uv run pytest
```

**Split long commands with `\` for readability.** Each flag or flag+value pair gets its own continuation line, indented. Positional parameters go on the final line.

Good:

```console
$ pipx install \
    --suffix=@next \
    --pip-args '\--pre' \
    --force \
    'agentgrep'
```

Bad:

```console
$ pipx install --suffix=@next --pip-args '\--pre' --force 'agentgrep'
```

### Changelog Conventions

These rules apply when authoring entries in `CHANGES`, which is rendered as the Sphinx changelog page. Modeled on Django's release-notes shape — deliverables get titles and prose, not bullets.

**Release entry boilerplate.** Every release header is `## agentgrep X.Y.Z (YYYY-MM-DD)`. The file opens with a `## agentgrep X.Y.Z (Yet to be released)` placeholder block fenced by `<!-- KEEP THIS PLACEHOLDER ... -->` and `<!-- END PLACEHOLDER ... -->` HTML comments — new release entries land immediately below the END marker, never above it.

**Open with a multi-sentence lead paragraph.** Plain prose, no italic. Open with the version as sentence subject (*"agentgrep X.Y.Z ships …"*) so the lead is self-contained when excerpted. Two to four sentences telling the reader what shipped and who cares — user-visible takeaways, not internal mechanism. Cross-reference detail docs with `{ref}` to keep the lead compact.

**Each deliverable is a section, not a bullet.** Inside `### What's new`, every distinct deliverable gets a `#### Deliverable title (#NN)` heading naming it in user vocabulary, followed by 1-3 prose paragraphs explaining what shipped. Don't wrap a paragraph in `- ` — bullets are for enumerable lists, not paragraph containers. Cross-link detail docs (`See {ref}\`foo\` for details.`) so prose stays focused.

**The deliverable test.** Before writing an entry, ask: "What's the deliverable, in user vocabulary?" If you can't answer in one sentence, the entry isn't ready. Mechanism (helper internals, byte counters, schema-validation locations) belongs in PR descriptions and code comments, not the changelog.

**Fixed subheadings**, in this order when present: `### Breaking changes`, `### Dependencies`, `### What's new`, `### Fixes`, `### Documentation`, `### Development`. Dev tooling (helper scripts, internal automation) lives under `### Development`. For breaking changes, show the migration path with concrete inline code (e.g. a `# Before` / `# After` fenced code block). Dependency floor bumps use the form ``Minimum `pkg>=X.Y.Z` (was `>=X.Y.W`)``.

**PR refs `(#NN)`** sit in each deliverable's `####` heading.

**When bullets are appropriate.** Catch-all sections (`### Fixes`, occasionally `### Documentation`) with 3+ genuinely small items use bullets — one line each, never paragraphs. If a bullet swells past two lines, promote it to a `#### Title (#NN)` heading with prose body.

**Anti-patterns.**

- Fragile metrics: token ceilings, third-party version pins, percent benchmarks, exact byte counts. Describe the *capability*, not the math.
- Internal jargon: private symbols (leading-underscore identifiers), algorithm names exposed for the first time, backend scaffolding.
- Walls of text dressed up as bullets.
- Buried breaking changes — they get their own subheading at the top of the entry.

**Always link autodoc'd APIs.** Any class, method, function, exception, attribute, or MCP tool slug that has its own rendered page must be cited via the appropriate role (`{class}`, `{meth}`, `{func}`, `{exc}`, `{attr}`, `{tooliconl}`) — never with plain backticks. Doc pages without explicit ref labels use `{doc}`. Plain backticks are correct for code syntax, env vars, parameter names, and file paths that aren't doc pages — anything without an autodoc destination.

**MyST roles.** Class references use `{class}` (e.g. `{class}\`~agentgrep.SearchRecord\``), methods use `{meth}`, functions use `{func}`, exceptions use `{exc}`, attributes use `{attr}`, MCP tool references use `{tooliconl}`, internal anchors use `{ref}`, doc-path links use `{doc}`.

**Summarization style.** When a user asks "what changed in the latest version?" or similar, lead with the entry's lead paragraph (paraphrased if needed), followed by each `####` deliverable heading under `### What's new` with a one-sentence summary. Cite `(#NN)` only if the user asks for source links. Don't invent versions, dates, or numbers not present in `CHANGES`. Don't quote line numbers or file offsets — those shift as the file evolves.

## Debugging Tips

When stuck in debugging loops:

1. **Pause and acknowledge the loop**
2. **Minimize to MVP**: Remove all debugging cruft and experimental code
3. **Document the issue** comprehensively for a fresh approach
4. **Format for portability** (using quadruple backticks)

## References

- Documentation: https://agentgrep.org/
- Source: https://github.com/tony/agentgrep
- FastMCP: https://github.com/jlowin/fastmcp
- MCP Specification: https://modelcontextprotocol.io/
- Textual: https://textual.textualize.io/

## AI Slop Prevention

Treat AI slop as **review-hostile noise**, not as proof that text or
code is wrong. The goal is to maximize information density by removing
artifacts that make the repository harder to trust or navigate.

### The Anti-Slop Rubric

Before committing, audit all AI-assisted changes for these noise
patterns:

- **AI Signatures:** Remove "Generated by", footers, conversational
  filler ("Certainly!", "Here is..."), unexplained emojis (🤖, ✨), and
  AI-tool metadata.
- **Brittle References:** Avoid hard-coded line numbers, fragile
  file/test counts, dated "as of" claims, bare SHAs, and local
  absolute paths unless they are strict evidentiary artifacts (e.g.,
  benchmark logs).
- **Diff Narration:** Do not restate what moved, was renamed, or was
  removed in artifacts the downstream reader holds: code, docstrings,
  README, CHANGES, PR descriptions, or release notes. The diff and
  commit message already carry this history.
- **Branch-Internal Narrative:** Do not mention intermediate branch
  states, abandoned approaches, or "no longer" behavior unless users
  of a published release actually experienced the old state (**The
  Published-Release Test**).
- **Low-Value Scaffolding:** Remove ownerless TODOs (`TODO: revisit`),
  unused future-proofing, debug artifacts, and defensive wrappers that
  do not protect a currently reachable failure mode.
- **Prose Inflation:** Replace generic AI "tells" like *comprehensive,
  robust, seamless, production-ready, leverage, delve, tapestry,* and
  *best practices* with concrete descriptions of behavior,
  constraints, or trade-offs.

### Preservation & Context

**When unsure, leave the text in place and ask.** Subjective cleanup
must never be a reason to remove load-bearing rationale.

- **Preserve the "Why":** You MUST NOT delete comments that document
  invariants, protocol constraints, platform quirks, security
  boundaries, and upstream workarounds.
- **Evidence is Immune:** Preserve exact counts, dates, and SHAs when
  they serve as evidence in benchmark results, release notes, stack
  traces, or lockfiles.
- **Behavior Over Inventory:** A useful description explains what
  changed for the *system or user*; it does not provide an inventory
  of files or functions the diff already shows.

### The Published-Release Test

Long-running branches accumulate tactical decisions — renames,
refactors, attempts-then-reverts. When deciding what counts as
branch-internal, use trunk or the parent branch as the baseline — not
intermediate states inside the current branch. Ask:

> Did users of the most recently published release ever experience
> this old name, old behavior, or bug?

If the answer is **no**, it is branch-internal narrative. Move it to
the commit message and describe only the final state in the artifact.

**Keep in shipped artifacts:**
- Deprecations and migration guides for symbols that actually shipped.
- `### Fixes` entries for bugs that affected users of a published
  release.
- Comments explaining *why the current code looks this way*
  (invariants, platform quirks) that make sense to a reader who never
  saw the previous version.

### Cleanup in Hindsight

When applying these rules retroactively from inside a feature branch,
first establish scope by diffing against the parent branch (or trunk)
to identify which commits this branch actually introduced. Then:

- **In-branch commits:** Prompt the user with two options: `fixup!`
  commits with `git rebase --autosquash` to address each causal commit
  at its source, or a single cleanup commit at branch tip.
- **Trunk/Parent commits:** Default to leaving them alone. Act only on
  explicit user instruction. If the user opts in, fold the cleanup
  into a single commit at branch tip; do not rewrite shared history.
- **Scope guard:** If cleaning prior slop would touch a colleague's
  work or expand the branch beyond its stated goal, stay in lane:
  protect the current goal and leave prior slop alone.
