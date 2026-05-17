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
- Pydantic models for every CLI/MCP output, with a pydantic-free fallback
- Full type safety (ty, strict warning-as-error)

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

## Code Architecture

agentgrep is a small surface — a single package with a CLI/TUI module and an MCP module that share the search engine:

```
src/agentgrep/
  __init__.py     # library, CLI, TUI — search engine and record types
  __main__.py     # `python -m agentgrep` entry point
  mcp.py          # FastMCP server, pydantic models, agentgrep-mcp entry
```

### Core Modules

1. **Library + CLI** (`src/agentgrep/__init__.py`)
   - Search engine across Codex, Claude Code, and Cursor stores
   - `SearchRecord` / `FindRecord` dataclasses + serializers
   - JSON envelope builder (`build_envelope`)
   - Pydantic-aware serialization via `maybe_build_pydantic()` with a pydantic-free fallback
   - Textual TUI (`run_ui`) for interactive browsing of normalized records
   - `main()` console script entry point (`agentgrep`)

2. **`python -m` entry** (`src/agentgrep/__main__.py`)
   - Thin wrapper so `python -m agentgrep` works alongside the console script

3. **MCP server** (`src/agentgrep/mcp.py`)
   - Builds the FastMCP server (`build_mcp_server`)
   - Pydantic models for every tool input/output (`SearchToolQuery`, `SearchToolResponse`, `FindToolQuery`, `FindToolResponse`, `SearchRecordModel`, `FindRecordModel`, `SourceRecordModel`, `BackendAvailabilityModel`, `CapabilitiesModel`, …)
   - `main()` console script entry point (`agentgrep-mcp`)

### Backend availability

agentgrep is opportunistic about its dependencies. `pydantic`, `textual`, and `fastmcp` are declared, but the search core also runs without them — the JSON path uses a typed-dict fallback so a CLI invocation works even when pydantic is unavailable. When adding code that imports an optional dependency, keep the fallback path intact and covered by a test (see `test_json_output_falls_back_without_pydantic` for the pattern).

## Testing Strategy

agentgrep uses pytest with `--doctest-modules` enabled by default (`testpaths = ["src/agentgrep", "tests"]`). Tests live under `tests/` and are split by entry point: `test_agentgrep.py` for the library/CLI/TUI and `test_agentgrep_mcp.py` for the MCP server.

### Testing Guidelines

1. **Use functional tests only**: Write tests as standalone functions, not classes. Avoid `class TestFoo:` groupings — use descriptive function names and file organization instead.

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
    # ... assert envelope shape ...
```

## Coding Standards

Key highlights:

### Imports

- **Use namespace imports for standard library modules**: `import enum` instead of `from enum import Enum`
  - **Exception**: `dataclasses` module may use `from dataclasses import dataclass, field` for cleaner decorator syntax
  - This rule applies to Python standard library only; third-party packages may use `from X import Y`
- **For typing**, use `import typing as t` and access via namespace: `t.NamedTuple`, etc.
- **Use `from __future__ import annotations`** at the top of all Python files

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

## Shipped vs. Branch-Internal Narrative

Long-running branches accumulate tactical decisions — renames,
refactors, attempts-then-reverts, intermediate states. Commit messages
and the diff hold *what changed* and *why*. Do not restate either in
artifacts the downstream reader holds: code, docstrings, README,
CHANGES, PR descriptions, release notes, migration guides.

When deciding what counts as branch-internal, use trunk or the parent
branch as the baseline — not intermediate states inside the current
branch.

**The Published-Release Test**

Before adding rename history, "previously" / "formerly" / "no longer
X" phrasing, "removed" / "moved" / "refactored" / "fixed" diff
paraphrases, or `### Fixes` entries to a user-facing surface, ask:

> Did users of the most recently published release ever experience
> this old name, old behavior, or bug?

If the answer is no, it is branch-internal narrative. Move it to the
commit message and describe only the current state in the artifact.

**Keep in shipped artifacts**

- Deprecations and migration guides for symbols that actually shipped.
- `### Fixes` entries for bugs that affected users of a published
  release.
- Comments explaining *why the current code looks this way* —
  invariants, platform quirks, upstream bug workarounds — that make
  sense to a reader who never saw the previous version.

**Default**: when in doubt, keep the artifact clean and put the story
in the commit.

### Cleanup in Hindsight

When applying this rule retroactively from inside a feature branch,
first establish scope by diffing against the parent branch (or trunk)
to identify which commits this branch actually introduced. Then:

- **Commits introduced in this branch** — prompt the user with two
  options: `fixup!` commits with `git rebase --autosquash` to address
  each causal commit at its source, or a single cleanup commit at
  branch tip. User chooses.
- **Commits already in trunk or a parent branch** — default to
  leaving them alone. Do not raise them as cleanup candidates; act
  only on explicit user instruction. If the user opts in, fold the
  cleanup into a single commit at branch tip and do not rewrite trunk
  or parent-branch history.
- **Scope guard** — if cleaning in-branch bleed would touch a
  colleague's in-flight work or expand the branch beyond its stated
  goal, default to staying in lane: protect the project's current
  goal, leave prior bleed alone, and don't introduce new bleed in the
  current change.
