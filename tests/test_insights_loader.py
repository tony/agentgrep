"""Tests for lazy backend loading and capability probes."""

from __future__ import annotations

import types
import typing as t

import pytest

from agentgrep.insights.loader import (
    BackendLoadError,
    BackendUnavailable,
    load_modules,
    module_available,
    probe_modules,
)


def _importer(available: dict[str, types.ModuleType]) -> t.Callable[[str], types.ModuleType]:
    """Return a fake importer that only knows ``available`` modules."""

    def _imp(name: str) -> types.ModuleType:
        if name in available:
            return available[name]
        raise ImportError(name)

    return _imp


def test_module_available_real_path_uses_find_spec() -> None:
    """With no injected importer, a stdlib module resolves and a fake one does not."""
    assert module_available("json") is True
    assert module_available("agentgrep_no_such_module_zzz") is False


def test_module_available_injected_importer() -> None:
    """An injected importer governs availability for tests."""
    importer = _importer({"present": types.ModuleType("present")})
    assert module_available("present", import_module=importer) is True
    assert module_available("absent", import_module=importer) is False


def test_probe_modules_reports_missing() -> None:
    """probe_modules returns the all-present flag and the missing names."""
    importer = _importer({"a": types.ModuleType("a")})
    present, missing = probe_modules(("a", "b"), import_module=importer)
    assert present is False
    assert missing == ("b",)


def test_load_modules_returns_loaded_by_name() -> None:
    """load_modules returns each requested module keyed by name."""
    mod_a = types.ModuleType("a")
    loaded = load_modules(("a",), level="ml", import_module=_importer({"a": mod_a}))
    assert loaded == {"a": mod_a}


def test_load_modules_raises_backend_unavailable_with_setup() -> None:
    """A missing module raises BackendUnavailable carrying the setup command."""
    with pytest.raises(BackendUnavailable) as excinfo:
        load_modules(
            ("missing",),
            level="ml",
            setup_command="uv pip install 'agentgrep[insights-ml]'",
            import_module=_importer({}),
        )
    assert excinfo.value.missing == ("missing",)
    assert excinfo.value.setup_command == "uv pip install 'agentgrep[insights-ml]'"
    assert excinfo.value.level == "ml"


def test_load_modules_wraps_non_import_errors() -> None:
    """A non-ImportError during import surfaces as BackendLoadError."""

    def _broken(name: str) -> types.ModuleType:
        message = "boom"
        raise RuntimeError(message)

    with pytest.raises(BackendLoadError):
        load_modules(("broken",), level="index", import_module=_broken)
