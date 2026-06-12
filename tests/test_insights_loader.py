"""Tests for typed lazy loading of optional insights backends."""

from __future__ import annotations

import types
import typing as t

import pytest

from agentgrep.insights_loader import (
    BackendUnavailable,
    load_backend_modules,
)


class LoaderCase(t.NamedTuple):
    """One backend import case."""

    test_id: str
    level: str
    modules: tuple[str, ...]


LOADER_CASES: tuple[LoaderCase, ...] = (
    LoaderCase(
        test_id="html-imports-template-modules",
        level="html",
        modules=("jinja2", "platformdirs"),
    ),
    LoaderCase(
        test_id="ml-imports-sklearn-submodules",
        level="ml",
        modules=("sklearn", "sklearn.feature_extraction.text", "sklearn.cluster"),
    ),
    LoaderCase(
        test_id="embeddings-imports-sentence-transformers",
        level="embeddings",
        modules=("sentence_transformers",),
    ),
    LoaderCase(
        test_id="index-imports-search-modules",
        level="index",
        modules=("sqlite_vec", "tantivy"),
    ),
    LoaderCase(
        test_id="llm-imports-local-model-modules",
        level="llm",
        modules=("llama_cpp", "httpx"),
    ),
)


@pytest.mark.parametrize("case", LOADER_CASES, ids=[case.test_id for case in LOADER_CASES])
def test_load_backend_modules_imports_only_requested_modules(case: LoaderCase) -> None:
    """The loader imports the exact optional module set requested by a backend."""
    imported: list[str] = []

    def fake_import_module(name: str) -> types.ModuleType:
        imported.append(name)
        return types.ModuleType(name)

    loaded = load_backend_modules(
        case.level,
        case.modules,
        import_module=fake_import_module,
    )

    assert tuple(loaded.modules) == case.modules
    assert tuple(imported) == case.modules
    for module_name in case.modules:
        assert loaded.require(module_name).__name__ == module_name


def test_load_backend_modules_reports_all_missing_modules() -> None:
    """Missing optional modules produce a typed configuration failure."""

    def fake_import_module(name: str) -> types.ModuleType:
        raise ModuleNotFoundError(name=name)

    with pytest.raises(BackendUnavailable) as exc_info:
        _ = load_backend_modules(
            "ml",
            ("sklearn", "sklearn.cluster"),
            import_module=fake_import_module,
        )

    assert exc_info.value.level == "ml"
    assert exc_info.value.missing_modules == ("sklearn", "sklearn.cluster")
    assert "agentgrep insights setup ml --install --yes" in str(exc_info.value)
