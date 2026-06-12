"""Tests for typed lazy loading of optional insights backends."""

from __future__ import annotations

import types
import typing as t

import pytest

from agentgrep.insights_loader import (
    BackendConfigurationError,
    BackendUnavailable,
    load_backend_modules,
)


class LoaderCase(t.NamedTuple):
    """One backend import case."""

    test_id: str
    level: str
    modules: tuple[str, ...]


class ConfigErrorCase(t.NamedTuple):
    """One runtime configuration failure case."""

    test_id: str
    level: str
    requirement: str
    examples: tuple[str, ...]
    expected_lines: tuple[str, ...]


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

CONFIG_ERROR_CASES: tuple[ConfigErrorCase, ...] = (
    ConfigErrorCase(
        test_id="llm-requires-model-or-local-endpoint-model",
        level="llm",
        requirement="local llama.cpp model path or Ollama model name",
        examples=(
            "agentgrep insights report --level llm --model /path/to/model.gguf",
            "agentgrep insights report --level llm --llm-backend ollama --model llama3",
        ),
        expected_lines=(
            "Insights backend 'llm' needs runtime configuration: "
            "local llama.cpp model path or Ollama model name.",
            "Try:",
            "  agentgrep insights report --level llm --model /path/to/model.gguf",
            "  agentgrep insights report --level llm --llm-backend ollama --model llama3",
        ),
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


@pytest.mark.parametrize(
    "case",
    CONFIG_ERROR_CASES,
    ids=[case.test_id for case in CONFIG_ERROR_CASES],
)
def test_backend_configuration_error_does_not_suggest_reinstall(
    case: ConfigErrorCase,
) -> None:
    """Installed backends with missing runtime inputs should not suggest reinstalling."""
    error = BackendConfigurationError(
        case.level,
        requirement=case.requirement,
        examples=case.examples,
    )

    assert error.level == case.level
    assert error.requirement == case.requirement
    assert error.examples == case.examples
    assert str(error).splitlines() == list(case.expected_lines)
    assert f"agentgrep insights setup {case.level}" not in str(error)
