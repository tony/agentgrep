"""Tests for optional insights report enrichers with fake lazy backends."""

from __future__ import annotations

import pathlib
import types
import typing as t

import pytest

import agentgrep
import agentgrep.insights as insights


class EnricherCase(t.NamedTuple):
    """One optional report enrichment case."""

    test_id: str
    level: insights.InsightsLevel
    expected_backend: str
    expected_data_key: str


ENRICHER_CASES: tuple[EnricherCase, ...] = (
    EnricherCase(
        test_id="html-renders-report-document",
        level="html",
        expected_backend="jinja2",
        expected_data_key="html",
    ),
    EnricherCase(
        test_id="ml-builds-topic-candidates",
        level="ml",
        expected_backend="scikit-learn",
        expected_data_key="topics",
    ),
    EnricherCase(
        test_id="embeddings-builds-semantic-groups",
        level="embeddings",
        expected_backend="sentence-transformers",
        expected_data_key="semantic_groups",
    ),
    EnricherCase(
        test_id="index-builds-local-index-summary",
        level="index",
        expected_backend="tantivy+sqlite-vec",
        expected_data_key="documents_indexed",
    ),
    EnricherCase(
        test_id="llm-builds-local-summary",
        level="llm",
        expected_backend="llama-cpp",
        expected_data_key="summary",
    ),
)


@pytest.mark.parametrize("case", ENRICHER_CASES, ids=[case.test_id for case in ENRICHER_CASES])
def test_build_report_applies_optional_enrichment(
    case: EnricherCase,
    tmp_path: pathlib.Path,
) -> None:
    """Each optional level can enrich a report through lazily imported modules."""
    model_path = tmp_path / "model.gguf"
    model_path.write_text("fake model", encoding="utf-8")

    report = insights.build_report(
        _records(),
        scope="prompts",
        requested_level=case.level,
        record_limit=10,
        sampled=True,
        model=str(model_path),
        import_module_for_backend=_fake_import_module,
    )

    payload = report.to_payload()
    assert payload["level"] == case.level
    assert payload["requested_level"] == case.level
    assert payload["skipped_enrichers"] == []
    enrichment = payload["enrichments"][0]
    assert enrichment["level"] == case.level
    assert enrichment["backend"] == case.expected_backend
    assert enrichment["status"] == "applied"
    assert case.expected_data_key in enrichment["data"]


def _records() -> list[agentgrep.SearchRecord]:
    return [
        _search_record(
            "Deploy docs and local model reports",
            timestamp="2026-06-01T00:00:00Z",
        ),
        _search_record(
            "Fix docs build and report clustering",
            timestamp="2026-06-02T00:00:00Z",
        ),
        _search_record(
            "Review local embeddings and index reports",
            timestamp="2026-06-03T00:00:00Z",
        ),
    ]


def _search_record(text: str, *, timestamp: str) -> agentgrep.SearchRecord:
    return agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.history",
        adapter_id="codex.history_jsonl.v1",
        path=pathlib.Path("/tmp/history.jsonl"),
        text=text,
        timestamp=timestamp,
    )


def _fake_import_module(name: str) -> types.ModuleType:
    modules = {
        "jinja2": _fake_jinja2_module(),
        "platformdirs": types.ModuleType("platformdirs"),
        "sklearn": types.ModuleType("sklearn"),
        "sklearn.feature_extraction.text": _fake_sklearn_text_module(),
        "sklearn.cluster": _fake_sklearn_cluster_module(),
        "sentence_transformers": _fake_sentence_transformers_module(),
        "sqlite_vec": _fake_sqlite_vec_module(),
        "tantivy": _fake_tantivy_module(),
        "llama_cpp": _fake_llama_cpp_module(),
        "httpx": types.ModuleType("httpx"),
    }
    try:
        return modules[name]
    except KeyError as exc:
        raise ModuleNotFoundError(name=name) from exc


def _fake_jinja2_module() -> types.ModuleType:
    module = types.ModuleType("jinja2")

    class Template:
        def __init__(self, source: str) -> None:
            self.source = source

        def render(self, **context: object) -> str:
            _ = (self.source, context)
            return "<!doctype html><title>Insights report</title>"

    module.__dict__["Template"] = Template
    return module


def _fake_sklearn_text_module() -> types.ModuleType:
    module = types.ModuleType("sklearn.feature_extraction.text")

    class FakeMatrix:
        shape = (3, 3)

    class TfidfVectorizer:
        def __init__(self, *, max_features: int, stop_words: str) -> None:
            _ = (max_features, stop_words)

        def fit_transform(self, texts: list[str]) -> FakeMatrix:
            _ = texts
            return FakeMatrix()

        def get_feature_names_out(self) -> list[str]:
            return ["deploy", "docs", "reports"]

    module.__dict__["TfidfVectorizer"] = TfidfVectorizer
    return module


def _fake_sklearn_cluster_module() -> types.ModuleType:
    module = types.ModuleType("sklearn.cluster")

    class MiniBatchKMeans:
        def __init__(
            self,
            *,
            n_clusters: int,
            random_state: int,
            n_init: str,
        ) -> None:
            self.n_clusters = n_clusters
            _ = (random_state, n_init)

        def fit_predict(self, matrix: object) -> list[int]:
            _ = matrix
            return [index % self.n_clusters for index in range(3)]

    module.__dict__["MiniBatchKMeans"] = MiniBatchKMeans
    return module


def _fake_sentence_transformers_module() -> types.ModuleType:
    module = types.ModuleType("sentence_transformers")

    class SentenceTransformer:
        def __init__(
            self,
            model_name_or_path: str,
            *,
            cache_folder: str | None = None,
            local_files_only: bool = False,
        ) -> None:
            assert pathlib.Path(model_name_or_path).exists()
            assert cache_folder is None
            assert local_files_only is True

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[float(index), 1.0] for index, _ in enumerate(texts)]

    module.__dict__["SentenceTransformer"] = SentenceTransformer
    return module


def _fake_sqlite_vec_module() -> types.ModuleType:
    module = types.ModuleType("sqlite_vec")

    def load(connection: object) -> None:
        _ = connection

    def serialize_float32(vector: list[float]) -> bytes:
        _ = vector
        return b"vector"

    module.__dict__["load"] = load
    module.__dict__["serialize_float32"] = serialize_float32
    return module


def _fake_tantivy_module() -> types.ModuleType:
    module = types.ModuleType("tantivy")

    class SchemaBuilder:
        def add_text_field(self, name: str, *, stored: bool) -> None:
            _ = (name, stored)

        def build(self) -> object:
            return object()

    class Document:
        def __init__(self) -> None:
            self.fields: dict[str, str] = {}

        def add_text(self, field_name: str, text: str) -> None:
            self.fields[field_name] = text

    class Writer:
        def add_document(self, document: Document) -> int:
            _ = document
            return 1

        def commit(self) -> int:
            return 1

    class Searcher:
        num_docs = 3
        num_segments = 1

    class Index:
        def __init__(self, schema: object) -> None:
            _ = schema

        def writer(self) -> Writer:
            return Writer()

        def reload(self) -> None:
            return None

        def searcher(self) -> Searcher:
            return Searcher()

    module.__dict__["SchemaBuilder"] = SchemaBuilder
    module.__dict__["Document"] = Document
    module.__dict__["Index"] = Index
    return module


def _fake_llama_cpp_module() -> types.ModuleType:
    module = types.ModuleType("llama_cpp")

    class Llama:
        def __init__(
            self,
            *,
            model_path: str,
            n_ctx: int,
            verbose: bool,
        ) -> None:
            assert pathlib.Path(model_path).exists()
            _ = (n_ctx, verbose)

        def create_chat_completion(
            self,
            *,
            messages: list[dict[str, str]],
            temperature: float,
            max_tokens: int,
        ) -> dict[str, object]:
            _ = (messages, temperature, max_tokens)
            return {"choices": [{"message": {"content": "Local summary"}}]}

    module.__dict__["Llama"] = Llama
    return module
