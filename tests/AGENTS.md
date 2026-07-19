# Test Suite Rules

These rules apply to every file under `tests/`.

## Purpose

Add a test only when it protects a critical user-visible or architectural
contract that is not already exercised by the protected tests under `src/` or
`docs/`. Prefer the cheapest stable layer that can prove the behavior.

Start with executable examples. For each critical boundary, prefer one happy
path and one meaningful failure path over exhaustive permutations of internal
implementation details.

## Cached Latency Budget

- The cached `uv run py.test` loop should finish in under 20 seconds and must
  finish in under 30 seconds.
- Every default-selected test must finish in 200 milliseconds or less,
  including its setup, call, and teardown phases.
- The first cold run used to install, import, compile, or populate a valid
  cache does not count toward these limits.
- Measure at least three subsequent serial runs and report the median. Do not
  use xdist to hide a slow test or an oversized default suite.
- A test that cannot meet the cached 200-millisecond limit must be optimized or
  marked `slow`. Essential slow tests are opt-in locally with
  `uv run py.test -m ""` and run in CI.

## Cache Safety

Cache only immutable harness artifacts that are expensive to construct and
safe to share. Cache keys must cover the artifact inputs, fixture schema,
Python version, and relevant dependency versions. A source, fixture, schema, or
dependency change must invalidate the cache.

Do not cache assertions, test outcomes, mutable application state, Textual app
instances, MCP client sessions, open files, event loops, or temporary paths.
Include one focused invalidation test for every cross-run cache.

## Preferred Coverage

- Public query parsing and evaluation contracts.
- One end-to-end CLI search example over a tiny synthetic store.
- MCP schema/annotation contracts plus one tool round trip.
- Engine cancellation, ordering, and result-shape invariants at the lowest
  layer that proves them.
- The deterministic Textual watchdog oracle. Use the repo-local pump skill for
  manual static review; add a `slow` mounted smoke only for a unique terminal
  bootstrap regression that Pilot cannot prove.
- Packaging, entry-point, and documentation examples that protect installation
  and setup behavior.

Use typed standalone test functions. Parametrized behavior tables should use a
typed `NamedTuple` with a stable `test_id` and explicit `ids=`.

## Avoid

- Repeating the same semantic matrix through library, CLI, MCP, and TUI layers.
- One mounted Textual app or FastMCP client session per small assertion.
- Subprocess tests for behavior that a direct typed API can prove.
- Snapshots of large internal structures when a small contract assertion is
  sufficient.
- Tests for private branches, formatting trivia, or implementation details
  without a concrete regression or public contract.
- Sleeps, retries, broad fuzz matrices, and large fixture stores in the default
  lane.

When a new test would break either latency budget, reduce its scope, share an
immutable cached harness, or move it to the explicit slow/CI lane before
merging.
