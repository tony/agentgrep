(adr-pure-python-rust-accelerator-compatibility)=

# ADR 0002: Pure Python/Rust accelerator module compatibility requirements

## Status

Accepted.

## Context

agentgrep is a Python project that may eventually use Rust to accelerate
selected modules, functions, or classes.

Native acceleration creates a compatibility risk: the Rust implementation can
accidentally become the real implementation, while the Python implementation
becomes incomplete, stale, or semantically different. That harms portability,
testability, and user trust. It can also make agentgrep harder to install in
environments without a Rust toolchain or without compatible binary wheels.

Python's standard library has a similar policy in PEP 399 for pure Python
modules with native accelerators. This ADR adapts that idea for agentgrep:
pure Python remains the reference implementation, and Rust exists only as an
optional drop-in accelerator.

## Decision

Every public API must have a pure Python implementation unless agentgrep
explicitly grants an exemption.

The pure Python implementation is the semantic source of truth. Rust
acceleration may be added for performance, but it must behave as a drop-in
replacement for the Python implementation as far as reasonably possible.

The Rust accelerator must pass the same behavioral tests as the pure Python
implementation. Rust-specific tests may be added, but they do not replace
shared compatibility tests.

The package must remain usable without the Rust extension.

This ADR is the canonical home for accelerator import/fallback rules, shared
Python/Rust compatibility-test expectations, the Python-only and Rust-enabled
CI matrix, and the accelerator pull request checklist. Always-loaded agent
instructions may point here, but they must not maintain a second copy of those
mechanics.

## Scope

This ADR applies to:

- Public functions
- Public classes
- Public methods and attributes
- Public constants whose value or type is part of the API contract
- Public module behavior
- Serialization, equality, hashing, ordering, iteration, context-manager, and
  async behavior where relevant
- Error behavior that users can observe

This ADR also applies to private Rust code when that code affects public
behavior.

This ADR governs drop-in accelerators as defined in
{ref}`adr-native-boundary-execution-architecture`: native code that
transparently replaces public Python behavior, so that removing the native
build changes nothing a user can observe except speed. Native engines and
workers are governed by {ref}`adr-native-boundary-execution-architecture` and
are not held to the exact-match requirements below. This ADR continues to
apply to any native component that presents itself as a drop-in replacement
for a public Python function, class, method, attribute, or module.

## Requirements

### Pure Python first

New public behavior must be implemented in Python before it is accelerated in
Rust.

Rust must not be the only implementation of a public API unless an exemption
is approved and documented.

Acceptable exemption cases are narrow. Examples include:

- APIs whose only purpose is to expose a Rust-only subsystem.
- Functionality that cannot reasonably be implemented in Python.
- Internal diagnostics, build hooks, or development-only helpers that are not
  public API.

Exemptions must be documented in the pull request and in the relevant module
or package documentation.

### Rust as companion accelerator

Rust acceleration is a companion implementation, not an independent API
surface.

Rust may replace selected Python functions, classes, or internals only after
the Python implementation has defined the expected public behavior.

Rust must not introduce:

- New public functions
- New public classes
- New public methods or attributes
- New accepted argument forms
- New return shapes
- Different validation rules
- Different mutation side effects
- Different ordering, equality, hashing, or serialization behavior
- Different exception classes for the same invalid input, unless approved and
  documented

### Optional accelerator

The project must import and run without the Rust extension.

When the Rust extension is unavailable, the package should fall back to
Python:

```python
from ._module_py import normalize, parse

_HAS_RUST_ACCELERATOR = False

try:
    from ._native import normalize as normalize
    from ._native import parse as parse
except ImportError:
    pass
else:
    _HAS_RUST_ACCELERATOR = True
```

Fallback code should catch `ImportError`, not broad `Exception`, unless there
is a specific and documented reason. Tests should not hide unexpected Rust
import failures.

### Shared compatibility tests

Every accelerated API must be tested against both implementations.

The shared test suite must run against:

1. The pure Python implementation with Rust disabled or absent.
2. The Rust-accelerated implementation when Rust is available.

Recommended `pytest` structure:

```python
import pytest

from agentgrep import _module_py

try:
    from agentgrep import _native
except ImportError:
    _native = None


@pytest.fixture(params=[_module_py, _native], ids=["python", "rust"])
def impl(request):
    if request.param is None:
        pytest.skip("Rust accelerator is not available")
    return request.param


def test_empty_input(impl):
    assert impl.parse("") == []


def test_invalid_input(impl):
    with pytest.raises(ValueError):
        impl.parse("\x00")
```

Tests must cover the behavior users rely on, including:

- Normal inputs
- Empty inputs
- Boundary values
- Invalid inputs
- Subclasses and duck-typed inputs where relevant
- Mutation and aliasing behavior
- Repeated calls
- Large inputs
- Unicode or binary edge cases where relevant
- Error paths
- Resource cleanup paths
- Serialization, equality, hashing, ordering, iteration, context-manager, or
  async behavior where relevant

### Duck typing preservation

Rust must preserve the input contract of the Python implementation.

If Python accepts any iterable, mapping, sequence, path-like object,
buffer-like object, subclass, or file-like object, Rust must not narrow that
behavior to a concrete type only.

Fast paths are allowed, but they must retain a correct generic path.

Acceptable:

```text
Rust uses a fast path for list[str], then falls back to generic iterable handling.
```

Unacceptable:

```text
Python accepts any iterable[str], but Rust accepts only list[str].
```

### Error behavior

Rust must raise the same Python exception classes as the Python
implementation wherever practical.

Rust panics must not cross the Python FFI boundary. Internal Rust errors must
be converted into Python exceptions.

The compatibility tests must verify important error paths.

### Documentation and type hints

Public documentation describes the public Python API, not the Rust
implementation.

Type hints, overloads, and stubs must remain accurate for the public API
regardless of whether Rust is installed.

Rust-only signatures must not leak into user-facing documentation or stubs.

### Packaging

The package must remain usable in environments without a Rust compiler or
compatible native wheel unless agentgrep explicitly approves a Rust-required
feature.

Packaging must support:

- Python-only operation
- Rust-accelerated operation when available
- Clear fallback behavior
- No import-time failure solely because Rust is unavailable

### CI

CI must include both code paths.

Minimum required jobs:

```text
Python-only job:
  - install without Rust or force the Python fallback
  - run the full shared behavioral test suite

Rust-enabled job:
  - build/install the Rust extension
  - run the same shared behavioral test suite
  - run Rust-specific tests, if any
```

The Python-only job is mandatory. A passing Rust-enabled job does not
compensate for a failing Python-only job.

### Unsafe Rust

`unsafe` Rust is allowed only when necessary.

Every `unsafe` block must have a nearby `SAFETY:` comment explaining:

1. Why `unsafe` is needed.
2. What invariants make it sound.
3. How those invariants are enforced.
4. Which tests cover the relevant edge cases, when applicable.

Example:

```rust
// SAFETY:
// `idx` is checked against `items.len()` immediately above.
// `items` is not mutated between the bounds check and access.
unsafe {
    items.get_unchecked(idx)
}
```

## Consequences

### Positive consequences

- agentgrep remains portable across environments where Rust is unavailable.
- Users receive the same behavior whether or not acceleration is installed.
- The Python implementation remains complete and useful for debugging,
  documentation, and alternative runtimes.
- Rust acceleration can be added without creating a second public API.
- CI detects semantic drift between Python and Rust implementations.

### Tradeoffs

- Contributors must maintain two implementations for accelerated behavior.
- Tests must be structured to exercise both paths.
- Some performance optimizations may be rejected if they narrow Python
  semantics.
- Build and packaging workflows must account for both Python-only and
  Rust-enabled modes.

### Risks

The main risk is semantic drift: Rust and Python implementations may diverge
over time. The mitigation is mandatory shared compatibility testing and
Python-first development.

Another risk is hidden fallback: broad exception handling can mask Rust
defects. The mitigation is narrow import fallback in runtime code and stricter
behavior in tests.

## Implementation guidance

Preferred module layout:

```tree
src/
  agentgrep/
    __init__.py
    module.py          # public API and accelerator selection
    _module_py.py      # pure Python reference implementation
    _native.*          # compiled Rust extension artifact
rust/
  Cargo.toml
  src/
    lib.rs
tests/
  test_module.py
  test_module_compat.py
```

Preferred public-module pattern:

```python
from ._module_py import Token, normalize, parse

_HAS_RUST_ACCELERATOR = False

try:
    from ._native import normalize as normalize
    from ._native import parse as parse
except ImportError:
    pass
else:
    _HAS_RUST_ACCELERATOR = True
```

Public Rust-only names must not be re-exported from the public module.

## Pull request checklist

A pull request that adds or modifies Rust acceleration must confirm this
implementation checklist:

```text
[ ] Public behavior exists first in pure Python.
[ ] Shared tests cover the Python behavior.
[ ] The same shared tests pass with Rust enabled.
[ ] The package imports and runs without Rust.
[ ] Rust exposes no extra public API.
[ ] Rust preserves duck-typed inputs accepted by Python.
[ ] Rust error behavior matches Python error behavior.
[ ] Type hints and documentation remain accurate.
[ ] Packaging impact is described.
[ ] Benchmarks or a clear performance rationale justify the accelerator.
[ ] Unsafe Rust, if any, is documented with SAFETY comments.
```

## Final position

Rust may make agentgrep faster. Rust must not make agentgrep less Pythonic,
less portable, less tested, less predictable, or less compatible.

The Python implementation defines the meaning of the public API. The Rust
implementation may make that meaning faster.
