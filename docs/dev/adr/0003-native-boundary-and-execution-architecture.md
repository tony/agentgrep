(adr-native-boundary-execution-architecture)=

# ADR 0003: Native boundary and execution architecture

## Status

Proposed.

## Context

agentgrep is a Python project that may eventually use Rust for native
implementation work. {ref}`adr-pure-python-rust-accelerator-compatibility`
governs one narrow shape of native code: a drop-in accelerator that replaces a
public Python callable, class, method, attribute, or module behavior while
preserving the observable Python API.

That model is necessary, but not sufficient. Python/Rust projects commonly
use native code in other shapes:

- an in-process engine that consumes a normalized plan or batch built by
  Python;
- an independent worker or binary that communicates with Python through a
  protocol;
- a Rust core exposed through one or more bindings.

Those shapes are not simple accelerators. They do not merely stand in for one
Python callable. They have different boundary costs, lifecycle risks,
packaging risks, and test obligations. Treating them as drop-in accelerators
hides those risks. Treating all native work as an engine or worker is also
wrong, because it can avoid the strict compatibility rules that apply to
public Python APIs.

This ADR is domain-agnostic. It defines how agentgrep chooses and governs a
Python/Rust boundary. Search-specific planning and execution semantics belong
in later ADRs.

## Decision

agentgrep adopts a default of no native code, and a cost-ordered ladder of
three native integration shapes for the exceptions: **accelerator**,
**engine**, and **worker**. Each native boundary is assigned to exactly one
shape before code is written. The assigned shape fixes the boundary rules,
test obligations, and governing ADR.

This ADR is the canonical home for native-boundary classification, engine and
worker obligations, packaging guidance, and the native change record.
Always-loaded agent instructions may summarize the default posture, but they
must point here instead of maintaining a second copy of the ladder or checklist.

### Default rule: no native code until measurement proves otherwise

Native code is not added because a path might be hot, because Rust is
available, or because a microbenchmark of a function in isolation looks good.
It is added only after measurement of the user-visible path shows a
performance, latency, jitter, scale, memory, reliability, or
platform-interface limit that the Python implementation cannot resolve
algorithmically or structurally, against a named baseline.

The baseline is trunk for active development comparisons and a tag or release
for release-facing claims. The measurement must make the relevant boundary
cost visible: how often Python crosses into native code, how much data crosses,
and how much work native code performs per crossing.

This default outranks every shape below. The shapes describe how to integrate
native code once the default is overcome, not whether native code is justified.

### Accelerator: drop-in for a public Python API

An accelerator replaces a pure Python public API with a native implementation
that preserves observable behavior. Python defines the meaning; Rust makes
that same meaning faster.

Accelerators are governed by
{ref}`adr-pure-python-rust-accelerator-compatibility`.

Test: removing the native build changes nothing a user can observe except
speed. The same callable, argument forms, return shapes, exceptions, mutation
behavior, equality, hashing, ordering, serialization, context-manager
behavior, and async behavior remain available.

Boundary: a public Python API to its native equivalent.

Tests: the same behavioral suite passes on both paths, with no tolerance
unless the public API already documents one.

An accelerator may be invoked once per public callable invocation. That does
not make per-item native crossings acceptable inside a larger internal loop
unless the measured user-visible path proves that this crossing pattern is
better than a batched boundary.

Do not dress a simple accelerator up as an engine to avoid
{ref}`adr-pure-python-rust-accelerator-compatibility` compatibility tests.

### Engine: in-process work over a plan, batch, or scoped native state

An engine consumes a normalized, typed plan or batch that Python builds,
performs bounded in-process work or owns explicitly scoped native state, and
returns compact results through coarse calls. Python remains the public
authoring surface and source of truth. The native side does not receive an
arbitrary Python object graph and does not call back into Python inside
per-item loops.

Test: the boundary is crossed once per coarse unit, such as per run, per
operation, per plan, per batch, or per flush. Native work happens in-process
and does not own an independent lifecycle or event loop hidden from Python.

Boundary: Python-owned normalization to native-owned plan, batch, or scoped
state.

Tests: semantic agreement with the Python path where behavior overlaps, plus
tests for plan normalization, error mapping, resource cleanup, lifecycle
boundaries, repeated use, and failure handling. Approximate reductions may use
documented numeric tolerances, but native code must not silently change public
semantics.

During heavy native work that touches no Python objects, the engine releases
the Python interpreter so other Python threads can make progress. The policy is
behavioral: heavy native work yields the interpreter. A clean implementation
may choose the exact API spelling for that binding.

Shape analogues: Polars-style Python plan construction with native execution;
pydantic-core-style schema construction with native validation.

### Worker: independent lifecycle behind a message-passing protocol

A worker runs independently of the Python caller and communicates by message
passing rather than by synchronous in-process calls. The distinguishing axis
is message passing vs. direct FFI, not the operating-system process. A
separate binary, a separate process, or a long-lived native background thread
that talks to Python over a channel can all be workers if they own an
independent lifecycle.

Test: the boundary is a versioned protocol or channel, and the native side
runs its own lifecycle. The two sides could be versioned, deployed, or
replaced independently as long as they agree on the protocol.

Boundary: Python orchestrator to independent worker over a protocol.

Tests: protocol schema tests, compatibility tests across protocol versions,
crash handling, timeout handling, cancellation handling, resource cleanup,
unsupported-capability rejection, and equivalence tests against the Python
runtime where behavior overlaps.

A worker crash, protocol mismatch, or unsupported capability is reported as an
operation failure. It is not masked by a silent switch to another execution
model.

A new worker execution mode and protocol require their own ADR unless an
existing approved protocol already covers the mode. Once a protocol exists,
additional workers implementing that protocol are ordinary feature work if
they do not change public semantics, packaging, or lifecycle behavior.

Boundary analogues: pure-native tools demonstrate the process-boundary and
no-hot-path-FFI model. A Python-orchestrated worker applies that idea through a
versioned protocol.

### Choosing the shape

Classify the boundary, not the component. A component with one boundary takes
the narrowest shape that honestly fits it. Evaluate in order and stop at the
first match:

1. Replaces a public Python API, observable only as speed? Use accelerator and
   {ref}`adr-pure-python-rust-accelerator-compatibility`.
2. Runs in-process over a typed plan, batch, or scoped native state, with
   coarse calls and no per-item Python callbacks? Use engine.
3. Runs independently behind a message-passing protocol or channel? Use
   worker.

A component that exposes more than one boundary must satisfy every shape it
touches. For example, a public callable backed by a background worker must
satisfy the accelerator rules for the callable surface and the worker rules
for the worker surface.

When a single boundary is genuinely ambiguous between adjacent shapes, take
the stricter higher shape. An accelerator/engine straddle is governed as an
engine. An engine/worker straddle is governed as a worker. A boundary that
fits none of the three is not designed yet. Design it before writing native
code.

## The boundary is the design

Design the boundary before writing native code. For an engine or worker, the
shape is wrong if Python crosses into native code for every item, event,
sample, node, record, or callback. Move the boundary up to a plan, batch,
buffer, typed state object, or protocol message.

Avoid this engine/worker shape:

```text
Python loop
  -> native: process one item
  -> native: update one accumulator
  -> native: compute one next step
```

Prefer this shape:

```text
Python configuration / plan / batch
  -> native work over the coarse unit
  -> compact result returns to Python
```

Accelerators are the exception: they are per-call drop-ins by nature. Even
then, internal per-item accelerator crossings must be justified by
user-visible measurement, not by a microbenchmark alone.

Native code must not call Python callbacks inside per-item loops unless a
later ADR approves that bridge and includes boundary-cost measurements.

## Keep user code in Python

Arbitrary user-provided Python code runs in Python. Native code may consume
data, plans, schemas, buffers, or declarative operations derived from user
input, but it does not acquire the right to execute arbitrary Python semantics
just because it is faster at a subset.

If a native path supports only a declarative subset, that subset is a separate
execution capability. It must be explicit, opt-in, documented, and checked
before execution starts. Unsupported features are rejected before execution.
They are not silently executed through Python callbacks or delegated to a
different execution model.

A later ADR may approve an embedded interpreter, compiled subset, callback
bridge, or other hybrid design, but that ADR must include the benchmark and
semantic analysis for the bridge.

## Separate logic from binding

Native logic and the mechanism that exposes it to Python are different
concerns. Keep native logic in a core that has no Python-binding dependency
when practical. Expose that core through thin bindings for in-process use and,
where applicable, through a worker for message-passing use.

This separation keeps the native core testable without Python, reduces
coupling to CPython ABI details, and allows one body of logic to be reached
through more than one boundary without entangling it with a single binding
mechanism.

The ADR adopts the principle, not a required crate count or directory layout.
agentgrep may choose the concrete structure that best fits its packaging and
build workflow.

## Packaging

Keep the base package installable, importable, and usable without native code
unless a later ADR explicitly approves a native-required feature. Missing
native code may remove acceleration or a separately documented native
capability. It must not remove the Python API or break import-time behavior.

For in-process accelerators and engines, prefer one package while the native
artifact remains optional and the wheel matrix is manageable.

A worker may be packaged as a separate artifact because it is a different
build target and may have a different lifecycle. Shipping it inside the main
package is acceptable while a single distribution remains practical. Split
distributions only on a documented trigger, such as sustained wheel-matrix or
build-maintenance pain that one distribution can no longer carry.

A stable ABI may be considered to reduce wheel-matrix pressure. It is not
required by default.

## Testing and benchmarks

Test obligations follow the boundary shape:

- Accelerators use
  {ref}`adr-pure-python-rust-accelerator-compatibility` shared compatibility
  tests.
- Engines use Python-vs-native semantic tests where behavior overlaps, plus
  boundary, lifecycle, cleanup, error-mapping, and tolerance tests.
- Workers use protocol contract tests, lifecycle tests,
  crash/timeout/cancellation tests, capability negotiation tests, and
  equivalence tests where behavior overlaps.

Across all shapes, justify native code with measurement of the user-visible
path against a named baseline. Make the boundary-crossing count visible.
Native tests never replace the Python-only suite. A green native-enabled job
does not compensate for a broken Python-only job.

## Native change record

A pull request that adds or changes native code includes this record. A
reviewer rejects the change if any field is missing, the shape is too weak for
the boundary, or the measurement does not justify native code.

```text
integration shape:       accelerator | engine | worker
user-visible behavior:   what this affects
boundary:                callable | plan/batch/state | message-passing
crossing frequency:      per public call | per run | per operation | per plan |
                         per batch | per flush | per protocol message
user Python in hot loop: no | bridged (cite approving ADR)
measurement + baseline:  user-visible-path measurement + named baseline
interpreter behavior:    for in-process native work, where heavy work yields Python
semantic comparison:     identity | documented tolerance | protocol equivalence
fallback behavior:       none | Python-only path | explicit user-selected fallback |
                         rejected before execution
capability check:        for subset/native modes, how unsupported features are rejected
protocol version:        for workers, schema/channel version
python-only preserved:   base installs, imports, runs, and passes its suite without native code
packaging impact:        none | same package native artifact | worker artifact | split package
unsafe Rust:             none | SAFETY comments and tests identified
```

For a worker, the record names the ADR that defines the protocol and
execution-mode boundary.

## Consequences

### Positive

- Native code has a measured reason to exist before implementation starts.
- The boundary shape, crossing frequency, and test burden are fixed before
  review.
- Drop-in accelerators remain governed by strict Python compatibility rules.
- Engines and workers can exist without pretending to be transparent
  accelerators.
- Native speedups are less likely to be erased by per-item FFI cost.
- Native logic can be tested separately from Python bindings.

### Tradeoffs

- agentgrep maintains boundary types, plan types, or protocol types where
  native engines or workers exist.
- Some native ideas are rejected because the boundary is too fine-grained or
  the packaging cost is too high.
- Engines and workers require more tests than pure Python alone.
- Declarative native subsets must be treated as explicit capabilities, not
  transparent acceleration.

### Risks

Shape inflation: a simple accelerator may be called an engine to avoid
{ref}`adr-pure-python-rust-accelerator-compatibility`. The ordered
classification rule mitigates this by checking the accelerator shape first.

Hidden lifecycle: a callable may quietly spawn a worker. The rule that each
boundary is governed by its own shape mitigates this.

Semantic drift: native behavior may diverge from Python behavior. The
mitigation is shared compatibility tests for accelerators, semantic comparison
tests for engines, and protocol contract tests for workers.

Silent fallback: native failures may be hidden by broad fallback behavior. The
mitigation is explicit fallback policy and tests that fail on unexpected native
import, runtime, or protocol errors.

Boundary creep: a coarse boundary may decay into per-item calls. The
mitigation is the native change record's crossing-frequency field and
benchmark requirement.

## Relationship to ADR 0002

{ref}`adr-pure-python-rust-accelerator-compatibility` governs drop-in
accelerators: native implementations that replace public Python APIs while
preserving observable behavior.

This ADR governs native boundaries that are not simple drop-ins: in-process
engines over plans, batches, or scoped state, and independent workers behind
protocols.

{ref}`adr-pure-python-rust-accelerator-compatibility` remains binding for any
public API exposed as a transparent replacement for pure Python behavior.
Engines and workers are not held to the exact-match model unless they also
expose an accelerator boundary, but they remain Python-first: Python-only
operation must continue to work unless a later ADR explicitly grants an
exemption, and native tests never replace the Python-only suite.

## Final position

Rust may make agentgrep faster, more predictable, or better able to integrate
with native platform boundaries. It must not make agentgrep less Pythonic,
less portable, less tested, less explicit, or less predictable.

The public Python API owns user semantics. Rust earns its place at measured,
coarse, explicit boundaries.
