"""Lazy backend loading and capability probes for insights enrichers.

This module is pure mechanism: it never imports an optional backend at
module load. Backends are resolved through an injectable
:data:`ImportModule` seam so tests can supply fake modules (and assert
the missing-dependency guidance) without installing PyTorch, tantivy,
LanceDB, or scikit-learn.

The typed error hierarchy lets callers distinguish *not installed*
(:class:`BackendUnavailableError`) from *installed but failed to import*
(:class:`BackendLoadError`), *misconfigured* (e.g. an un-provisioned
model — :class:`BackendConfigurationError`), and *ran but raised*
(:class:`BackendRuntimeError`). Each carries the precise next command.
"""

from __future__ import annotations

import collections.abc as cabc
import importlib
import importlib.util
import typing as t
from dataclasses import dataclass

# A loaded backend is an optionally-present third-party module that cannot be
# statically typed, so resolved modules are intentionally ``Any``.
ImportModule = cabc.Callable[[str], t.Any]
"""A drop-in for :func:`importlib.import_module` (the injectable seam)."""


def default_import_module() -> ImportModule:
    """Return the real :func:`importlib.import_module`."""
    return importlib.import_module


@dataclass(frozen=True, slots=True)
class BackendPolicy:
    """Caller policy for what a backend may do during a report run."""

    allow_download: bool = False
    allow_network: bool = False


class BackendError(Exception):
    """Base class for backend resolution and execution failures."""

    def __init__(
        self,
        message: str,
        *,
        level: str,
        setup_command: str | None = None,
    ) -> None:
        super().__init__(message)
        self.level = level
        self.setup_command = setup_command


class BackendUnavailableError(BackendError):
    """A required optional dependency is not installed."""

    def __init__(
        self,
        *,
        level: str,
        missing: cabc.Sequence[str],
        setup_command: str | None = None,
    ) -> None:
        joined = ", ".join(missing)
        super().__init__(
            f"insights level {level!r} needs missing package(s): {joined}",
            level=level,
            setup_command=setup_command,
        )
        self.missing = tuple(missing)


class BackendLoadError(BackendError):
    """An optional dependency is installed but failed to import."""


class BackendConfigurationError(BackendError):
    """A backend is installed but misconfigured (e.g. model not provisioned)."""


class BackendRuntimeError(BackendError):
    """A backend imported and started but failed while running."""


def module_available(name: str, *, import_module: ImportModule | None = None) -> bool:
    """Return whether ``name`` can be resolved as an importable module.

    With no injected importer (the production path) this uses
    :func:`importlib.util.find_spec`, which *locates* a top-level package
    without executing it — so probing a level's availability never pulls
    PyTorch, tantivy, or scikit-learn into memory just to populate the
    ``levels`` field of a builtin report.

    When ``import_module`` is injected (the test seam) the probe defers to
    it: an :class:`ImportError` means *not installed*. This lets tests
    model availability with fake modules.
    """
    if import_module is not None:
        try:
            import_module(name)
        except ImportError:
            return False
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except ImportError, ValueError:
        return False


def probe_modules(
    modules: cabc.Sequence[str],
    *,
    import_module: ImportModule | None = None,
) -> tuple[bool, tuple[str, ...]]:
    """Return ``(all_present, missing_modules)`` for ``modules``."""
    importer = import_module or default_import_module()
    missing = tuple(name for name in modules if not module_available(name, import_module=importer))
    return (not missing), missing


def load_modules(
    modules: cabc.Sequence[str],
    *,
    level: str,
    setup_command: str | None = None,
    import_module: ImportModule | None = None,
) -> dict[str, t.Any]:
    """Import every module in ``modules`` and return them by name.

    Raises
    ------
    BackendUnavailableError
        When any module is not installed (``ImportError``).
    BackendLoadError
        When a module is installed but raises a non-import error while
        importing.
    """
    importer = import_module or default_import_module()
    loaded: dict[str, t.Any] = {}
    missing: list[str] = []
    for name in modules:
        try:
            loaded[name] = importer(name)
        except ImportError:
            missing.append(name)
        except Exception as exc:
            message = f"insights level {level!r} failed to import {name!r}: {exc}"
            raise BackendLoadError(message, level=level, setup_command=setup_command) from exc
    if missing:
        raise BackendUnavailableError(
            level=level,
            missing=missing,
            setup_command=setup_command,
        )
    return loaded
