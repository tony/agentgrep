(adr-native-boundary-execution-architecture)=

# ADR 0003: Native boundary and execution architecture

## Status

Proposed.

## Context

agentgrep is Python-first but may use native code for measured hot paths or
platform boundaries. ADR 0002 governs one narrow case: a native implementation
that transparently replaces a public Python API. Native engines and independent
workers have different lifecycle, protocol, packaging, and testing risks and
must not be mislabeled to avoid those obligations.

## Decision

The default is no native code. A native boundary is justified only when a
measurement of a user-visible path against a named Python baseline shows a
limit that cannot reasonably be removed through algorithmic or structural
Python changes. The measurement includes boundary-crossing cost and the amount
of work performed per crossing.

Every approved native boundary is classified as exactly one of these shapes:

### Accelerator

An accelerator transparently replaces a public Python callable or type. The
Python implementation remains the semantic source of truth, and removing the
native artifact changes no observable behavior except performance.

Accelerators follow {ref}`ADR 0002
<adr-pure-python-rust-accelerator-compatibility>`, including shared behavioral
tests and Python-only availability. A component cannot call itself an engine to
avoid public compatibility requirements.

### Engine

An engine performs in-process work over a normalized plan, batch, buffer, or
explicitly scoped state. Python owns public normalization and semantics. The
boundary is coarse enough that per-record callbacks or repeated Python/native
crossings do not erase the measured benefit.

Heavy native work that does not require Python objects permits Python threads
to make progress. Engines map errors and resource lifecycle into typed Python
outcomes and preserve the Python path's semantics wherever the capabilities
overlap. A documented approximate native operation may define a tolerance, but
it must not silently alter an exact public operation.

### Worker

A worker owns an independent lifecycle behind a versioned message-passing
protocol. It may be a process, binary, remote provider, or long-lived native
thread; message passing and independent lifecycle, rather than operating-system
placement, define the shape.

A worker protocol defines capability negotiation, typed failures, cleanup,
crash behavior, cancellation, and version compatibility. Caller cancellation
and a whole-request deadline are shared lifecycle outcomes. A provider or
worker operation timeout is a typed operation outcome and does not masquerade
as caller cancellation.

A new worker protocol or execution mode requires a focused ADR unless an
existing adopted protocol already covers it.

### Boundary rules

Classify the boundary, not the component. A component that exposes multiple
boundary shapes satisfies each applicable contract. An ambiguous boundary uses
the stricter adjacent shape; a boundary that fits none of the three is not
ready for implementation.

Arbitrary user Python executes in Python. Native code may consume declarative
plans or data derived from user input, but an unsupported feature is rejected
before execution rather than silently delegated through per-item callbacks or
another execution model.

Native logic should remain separable from a particular Python binding when
practical. This is an architectural principle, not a required crate or
directory layout.

The base package remains installable, importable, and usable without optional
native artifacts unless a later ADR explicitly approves a native-required
feature. Packaging may keep an artifact in the main distribution or split a
worker when build and lifecycle evidence justify it.

### Semantic equivalence across transports

Execution transport never owns query or result semantics. Inline, thread,
process, worker, native, and provider drivers consume equivalent normalized
plans and return typed outcomes. For the same request, validated snapshot, and
equivalent source outcomes, transport and scheduling do not change logical
membership, representative selection, order, status, coverage, or deterministic
work accounting. Progress timing and physical-performance observations may
differ, and cancellation may shorten an otherwise canonical emitted prefix.

This requirement is logical equivalence, not byte identity for opaque cursor
tokens, diagnostic timestamps, or other intentionally non-semantic values.

### Verification

Test obligations follow the boundary:

- accelerators share the Python compatibility suite;
- engines test plan normalization, semantic equivalence, lifecycle, cleanup,
  error mapping, and documented tolerances; and
- workers test the protocol, version negotiation, failure, timeout,
  cancellation, cleanup, and overlapping semantic behavior.

Every native path retains a passing Python-only suite. Benchmarks exercise the
user-visible path and report enough boundary information to justify the chosen
shape.

## Relationships

- ADR 0002 owns transparent Python/Rust accelerator compatibility.
- ADR 0004 owns query plans, execution drivers, events, status, and
  cancellation semantics.
- ADR 0014 owns collector order, representative selection, deduplication, and
  stable emission regardless of transport.

## Consequences

Native work receives a measured reason, an explicit boundary, and the test
burden appropriate to its lifecycle. Python remains the public authoring and
semantic surface while engines and workers remain possible where a transparent
accelerator is the wrong shape.

The cost is additional plan or protocol design and broader lifecycle testing.
Some native ideas will be rejected because their boundary is too fine-grained,
their packaging burden exceeds their demonstrated benefit, or they cannot
preserve Python-only behavior.
