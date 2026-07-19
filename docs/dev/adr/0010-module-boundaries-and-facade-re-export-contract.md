(adr-module-boundaries-and-facade-re-export-contract)=

# ADR 0010: Module boundaries and the facade re-export contract

## Status

Proposed.

## Context

`src/agentgrep/__init__.py` had grown into a single ~7,960-line module. The
`cli/`, `query/`, `_engine/`, `mcp/`, and `ui/` packages were carved out earlier,
but the implementations they depend on — record types, parsers, discovery,
matching, progress, the search/find engine — stayed in the package facade. Every
one of those packages then imported *back up* into the facade
(`import agentgrep`), so the dependency graph pointed the wrong way: the engine,
which ADR 0004 says owns planning and execution, reached into the facade for its
own logic; two frontends reached back for a single text helper; and the facade
re-imported its own submodules at the bottom under `# noqa: E402`.

Two consequences followed. First, there was no real public/private boundary: the
facade declared no `__all__`, so every top-level symbol leaked as public API even
though ADR 0006 calls the public surface compatibility-sensitive. Second, the
import graph was fragile — a top-level `import agentgrep` in a submodule worked
only by load-order luck.

The facade has since been emptied into conventionally-named, single-responsibility
modules: `records`, `_types`, `_text`, `readers`, `progress`, `cli/help_theme`,
`adapters`, `discovery`, and `_engine/orchestration`. `__init__.py` is now a thin
re-export shim plus `main`, `run_ui`, and `build_streaming_ui_app`. This ADR
records the boundary contract that keeps it that way.

## Decision

agentgrep treats its internal module graph as a one-direction dependency layering,
and the package facade as a compatibility re-export shim that owns no domain
behavior.

### Dependency direction

Dependencies flow one way, from the leaves toward the facade:

```
records / _types / _text   (dependency-free domain vocabulary + presentation)
        |
readers                     (read-only file, sqlite, protobuf, subprocess I/O floor)
        |
adapters / discovery        (per-agent parsers + normalization; store discovery)
        |
_engine                     (planning, execution, orchestration, matching)
        |
cli / ui / mcp / query      (frontends; depend down only)
        |
__init__.py                 (thin re-export shim + main())
```

No module under `records`, `readers`, `adapters`, `discovery`, `_engine`,
`query`, `cli`, or `ui` may import the `agentgrep` *facade* (a bare
`import agentgrep` or a facade-level `from agentgrep import X`). Modules import
their dependencies directly from the owning module (`from agentgrep.records import
SearchRecord`). Importing a sibling *submodule* (for example the typed
`agentgrep.events` stream, or `from agentgrep._engine.planning import ...`) is
permitted, because it names a concrete lower module rather than the facade's
re-export namespace.

A guard test (`test_engine_does_not_import_facade` and siblings) enforces the
rule by scanning each package for a bare `import agentgrep`. Removing the residual
facade imports from the satellite packages and landing that guard is the final
step of the migration; everything below it is already in place.

### The facade is a re-export shim

`__init__.py` re-exports the public names from the owning modules so
`import agentgrep; agentgrep.SearchRecord` stays byte-stable, and keeps only the
process entry points (`main`), the TUI launchers (`run_ui`,
`build_streaming_ui_app`), and the interpreter setup. It contains no parsing,
discovery, matching, or orchestration logic. Re-exports that pull from a
satellite *package* whose `__init__` imports the facade entry point stay in the
trailing `# noqa: E402` block so they resolve after `main` is defined.

### Module docstrings state the single responsibility

Every module's docstring opens by naming its one responsibility and the layers it
must not import (for example, `records.py` is dependency-free; `progress.py` sits
below the engine that drives it and the frontends that render it). The docstring
is the human-readable half of the boundary; the guard test is the enforced half.

### The public surface is `__all__`

The facade declares an explicit `__all__`; its union with the trailing
compatibility re-exports is the public surface ADR 0006 governs. New modules
declare `__all__` as they are created. Sealing the facade `__all__` is treated as
a deliberate compatibility step rather than a refactor freebie, because adding
`__all__` to a module that lacked one narrows `from agentgrep import *`.

### Required-dependency import rules

Pydantic is a required dependency and provides schema and validation adapters at
explicit MCP and event boundaries. CLI JSON and NDJSON output calls the direct
TypedDict serializers rather than revalidating their payloads through
`TypeAdapter`. Heavy required frontend dependencies stay behind lazy, call-site
imports, while optional accelerators use guarded imports. The `agentgrep --help`
cold-start budget is preserved by keeping the query registry, the events module,
and the per-agent parsers off the eager `import agentgrep` path;
`tests/test_import_time.py` pins it.

### No native rewrite is implied

This is a pure-Python module-boundary decision. ADR 0002 and ADR 0003 still hold:
pure Python is the semantic source of truth, and any Rust accelerator or native
engine must be justified by measurement. The boundary in fact *helps* a future
accelerator drop in behind `records`/`readers` without the facade noticing.

## Consequences

### Positive

- The dependency graph has one direction; the engine owns its logic where ADR
  0004 places it.
- The facade becomes a readable, reviewable public-surface contract instead of an
  8,000-line grab bag.
- A Rust accelerator can replace a leaf module without touching callers.
- Strict `ty` checks gain a real public/private boundary.

### Tradeoffs

- Re-exports must be maintained so old import paths stay byte-stable; identity
  tests (`agentgrep.X is agentgrep.<module>.X`) prove each move neutral.
- Tests that monkeypatch an engine helper must patch it where the caller resolves
  it (its owning module) rather than on the facade.

### Risks

- The boundary can erode if a helper is dropped into `__init__.py` "just for now."
  The guard test is the mitigation.

## Relationship to other ADRs

ADR 0001 owns storage-version evidence. ADR 0004 owns planning, execution, the
event streams, and result payloads — so the logic those describe must *live* in
`_engine`. ADR 0006 owns the public CLI/MCP surface vocabulary — so the facade's
re-export set is that surface made literal. ADR 0002 and ADR 0003 own the native
boundary policy this decision is careful not to disturb.

## Final position

agentgrep is a layered Python package with a thin compatibility facade. The
implementation lives in single-responsibility modules that depend downward only;
`__init__.py` re-exports their public names and runs the program. Implementation
libraries adapt that surface; they do not define it.
