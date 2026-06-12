"""Tests for optional insights report enrichers with fake lazy backends."""

from __future__ import annotations

import json
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


class ProgressEvent(t.NamedTuple):
    """One recorded insights-progress callback."""

    name: str
    backend: str
    model: str
    endpoint: str
    chunk_count: int
    char_count: int


class StreamRequest(t.NamedTuple):
    """One fake HTTP streaming request."""

    method: str
    url: str
    payload: dict[str, object]


class TimeoutConfig(t.NamedTuple):
    """One fake httpx timeout configuration."""

    connect: float
    read: float | None
    write: float
    pool: float


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


def test_build_report_streams_ollama_summary_and_reports_progress() -> None:
    """The Ollama backend streams chunks and exposes chunk progress."""
    httpx = _fake_streaming_httpx_module(
        (
            json.dumps({"message": {"content": "Local "}, "done": False}),
            json.dumps({"message": {"content": "summary"}, "done": True}),
        ),
    )
    progress = RecordingInsightsProgress()

    report = insights.build_report(
        _records(),
        scope="prompts",
        requested_level="llm",
        record_limit=10,
        sampled=True,
        model="llama3",
        llm_backend="ollama",
        import_module_for_backend=_fake_ollama_import_module(httpx),
        progress=progress,
    )

    payload = report.to_payload()
    enrichment = payload["enrichments"][0]
    assert enrichment["backend"] == "ollama"
    assert enrichment["data"]["summary"] == "Local summary"

    requests = t.cast("list[StreamRequest]", httpx.__dict__["requests"])
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url == "http://127.0.0.1:11434/api/chat"
    assert request.payload["model"] == "llama3"
    assert request.payload["stream"] is True
    timeouts = t.cast("list[TimeoutConfig]", httpx.__dict__["timeouts"])
    assert timeouts == [TimeoutConfig(connect=5.0, read=None, write=30.0, pool=5.0)]

    assert [event.name for event in progress.events] == [
        "started",
        "waiting",
        "chunk",
        "chunk",
        "finished",
    ]
    assert progress.events[-1].chunk_count == 2
    assert progress.events[-1].char_count == len("Local summary")


def test_build_report_streams_litert_lm_summary_and_reports_progress(
    tmp_path: pathlib.Path,
) -> None:
    """The LiteRT-LM backend streams chunks through the in-process Python API."""
    model_path = tmp_path / "model.litertlm"
    model_path.write_text("fake model", encoding="utf-8")
    litert_lm = _fake_litert_lm_module(
        (
            {"content": [{"type": "text", "text": "Local "}]},
            {"content": [{"type": "text", "text": "summary"}]},
        ),
    )
    progress = RecordingInsightsProgress()

    report = insights.build_report(
        _records(),
        scope="prompts",
        requested_level="llm",
        record_limit=10,
        sampled=True,
        model=str(model_path),
        llm_backend="litert-lm",
        import_module_for_backend=_fake_litert_lm_import_module(litert_lm),
        progress=progress,
    )

    payload = report.to_payload()
    enrichment = payload["enrichments"][0]
    assert enrichment["backend"] == "litert-lm"
    assert enrichment["data"]["summary"] == "Local summary"
    assert enrichment["data"]["model"] == str(model_path)

    engines = t.cast("list[dict[str, object]]", litert_lm.__dict__["engines"])
    assert engines == [
        {
            "model_path": str(model_path),
            "backend": "cpu",
            "max_num_tokens": 2048,
        },
    ]
    prompts = t.cast("list[str]", litert_lm.__dict__["prompts"])
    assert len(prompts) == 1
    assert "Top terms:" in prompts[0]
    assert [event.name for event in progress.events] == [
        "started",
        "waiting",
        "chunk",
        "chunk",
        "finished",
    ]
    assert progress.events[0].backend == "litert-lm"
    assert progress.events[-1].chunk_count == 2
    assert progress.events[-1].char_count == len("Local summary")


def test_build_report_supports_released_litert_lm_stream_signature(
    tmp_path: pathlib.Path,
) -> None:
    """The LiteRT-LM adapter supports PyPI releases without token-budget kwargs."""
    model_path = tmp_path / "model.litertlm"
    model_path.write_text("fake model", encoding="utf-8")
    litert_lm = _fake_litert_lm_module(
        ({"content": [{"type": "text", "text": "Released wheel"}]},),
        supports_max_output_tokens=False,
    )

    report = insights.build_report(
        _records(),
        scope="prompts",
        requested_level="llm",
        record_limit=10,
        sampled=True,
        model=str(model_path),
        llm_backend="litert-lm",
        import_module_for_backend=_fake_litert_lm_import_module(litert_lm),
        progress=RecordingInsightsProgress(),
    )

    payload = report.to_payload()
    enrichment = payload["enrichments"][0]
    assert enrichment["backend"] == "litert-lm"
    assert enrichment["data"]["summary"] == "Released wheel"


def test_build_report_cancels_unbounded_litert_lm_stream(
    tmp_path: pathlib.Path,
) -> None:
    """The released LiteRT-LM stream path is capped and drained after cancel."""
    model_path = tmp_path / "model.litertlm"
    model_path.write_text("fake model", encoding="utf-8")
    litert_lm = _fake_litert_lm_module(
        tuple({"content": [{"type": "text", "text": "x"}]} for _ in range(100)),
        supports_max_output_tokens=False,
    )

    report = insights.build_report(
        _records(),
        scope="prompts",
        requested_level="llm",
        record_limit=10,
        sampled=True,
        model=str(model_path),
        llm_backend="litert-lm",
        import_module_for_backend=_fake_litert_lm_import_module(litert_lm),
        progress=RecordingInsightsProgress(),
    )

    payload = report.to_payload()
    enrichment = payload["enrichments"][0]
    assert enrichment["data"]["summary"] == "x" * 64
    assert litert_lm.__dict__["cancellations"] == 1
    assert litert_lm.__dict__["drains_after_cancel"] == 1


def test_build_report_rejects_malformed_litert_lm_stream(
    tmp_path: pathlib.Path,
) -> None:
    """Malformed LiteRT-LM streaming chunks become actionable runtime errors."""
    model_path = tmp_path / "model.litertlm"
    model_path.write_text("fake model", encoding="utf-8")
    litert_lm = _fake_litert_lm_module(({"content": [{"type": "image"}]},))

    with pytest.raises(insights.BackendRuntimeError) as exc_info:
        insights.build_report(
            _records(),
            scope="prompts",
            requested_level="llm",
            record_limit=10,
            sampled=True,
            model=str(model_path),
            llm_backend="litert-lm",
            import_module_for_backend=_fake_litert_lm_import_module(litert_lm),
            progress=RecordingInsightsProgress(),
        )

    error = exc_info.value
    assert "empty response from LiteRT-LM model" in error.detail
    assert "agentgrep insights setup llm --llm-backend litert-lm --install --yes" in (
        error.examples
    )


def test_build_report_rejects_malformed_ollama_stream() -> None:
    """Malformed Ollama streaming JSON becomes an actionable runtime error."""
    httpx = _fake_streaming_httpx_module(("not-json",))

    with pytest.raises(insights.BackendRuntimeError) as exc_info:
        insights.build_report(
            _records(),
            scope="prompts",
            requested_level="llm",
            record_limit=10,
            sampled=True,
            model="llama3",
            llm_backend="ollama",
            import_module_for_backend=_fake_ollama_import_module(httpx),
            progress=RecordingInsightsProgress(),
        )

    error = exc_info.value
    assert "invalid streaming response from http://127.0.0.1:11434" in error.detail
    assert "ollama serve" in error.examples


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
    }
    try:
        return modules[name]
    except KeyError as exc:
        raise ModuleNotFoundError(name=name) from exc


class RecordingInsightsProgress:
    """Test progress recorder with the insights progress surface."""

    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def llm_started(self, *, backend: str, model: str, endpoint: str) -> None:
        """Record an LLM-start callback."""
        self.events.append(
            ProgressEvent(
                "started",
                backend,
                model,
                endpoint,
                0,
                0,
            ),
        )

    def llm_waiting(self, *, backend: str, model: str, endpoint: str) -> None:
        """Record an LLM-waiting callback."""
        self.events.append(
            ProgressEvent(
                "waiting",
                backend,
                model,
                endpoint,
                0,
                0,
            ),
        )

    def llm_chunk(
        self,
        *,
        backend: str,
        model: str,
        chunk_count: int,
        char_count: int,
    ) -> None:
        """Record a streamed chunk callback."""
        self.events.append(
            ProgressEvent(
                "chunk",
                backend,
                model,
                "",
                chunk_count,
                char_count,
            ),
        )

    def llm_finished(
        self,
        *,
        backend: str,
        model: str,
        chunk_count: int,
        char_count: int,
    ) -> None:
        """Record an LLM-complete callback."""
        self.events.append(
            ProgressEvent(
                "finished",
                backend,
                model,
                "",
                chunk_count,
                char_count,
            ),
        )


def _fake_ollama_import_module(httpx: types.ModuleType) -> insights.ImportModule:
    def fake_import_module(name: str) -> types.ModuleType:
        if name == "httpx":
            return httpx
        raise ModuleNotFoundError(name=name)

    return fake_import_module


def _fake_litert_lm_import_module(litert_lm: types.ModuleType) -> insights.ImportModule:
    def fake_import_module(name: str) -> types.ModuleType:
        if name == "litert_lm":
            return litert_lm
        raise ModuleNotFoundError(name=name)

    return fake_import_module


def _fake_streaming_httpx_module(lines: t.Sequence[str]) -> types.ModuleType:
    module = types.ModuleType("httpx")
    requests: list[StreamRequest] = []
    timeouts: list[TimeoutConfig] = []

    class FakeHTTPError(Exception):
        """Base fake HTTP transport error."""

    class FakeTimeoutException(FakeHTTPError):
        """Fake timeout transport error."""

    class Timeout:
        """Fake ``httpx.Timeout`` value."""

        def __init__(
            self,
            *,
            connect: float,
            read: float | None,
            write: float,
            pool: float,
        ) -> None:
            self.config = TimeoutConfig(
                connect=connect,
                read=read,
                write=write,
                pool=pool,
            )

    class FakeStreamResponse:
        """Context manager for streaming response lines."""

        def __enter__(self) -> t.Self:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: types.TracebackType | None,
        ) -> bool:
            _ = (exc_type, exc, traceback)
            return False

        def raise_for_status(self) -> None:
            """Pretend the HTTP status was successful."""

        def iter_lines(self) -> t.Iterator[str]:
            return iter(lines)

    class FakeClient:
        """Minimal context-manager client with ``httpx.Client.stream``."""

        def __init__(self, *, timeout: Timeout) -> None:
            timeouts.append(timeout.config)

        def __enter__(self) -> t.Self:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: types.TracebackType | None,
        ) -> bool:
            _ = (exc_type, exc, traceback)
            return False

        def stream(self, method: str, url: str, *, json: object) -> FakeStreamResponse:
            requests.append(
                StreamRequest(
                    method=method,
                    url=url,
                    payload=t.cast("dict[str, object]", json),
                ),
            )
            return FakeStreamResponse()

    module.__dict__.update(
        {
            "Client": FakeClient,
            "HTTPError": FakeHTTPError,
            "Timeout": Timeout,
            "TimeoutException": FakeTimeoutException,
            "requests": requests,
            "timeouts": timeouts,
        },
    )
    return module


def _fake_litert_lm_module(
    chunks: t.Sequence[dict[str, t.Any]],
    *,
    supports_max_output_tokens: bool = True,
) -> types.ModuleType:
    module = types.ModuleType("litert_lm")
    engines: list[dict[str, object]] = []
    prompts: list[str] = []
    cancellations = 0
    drains_after_cancel = 0

    class CPU:
        def get_name(self) -> str:
            return "cpu"

    class SamplerConfig:
        def __init__(self, *, temperature: float) -> None:
            self.temperature = temperature

    class BaseFakeConversation:
        def __init__(self) -> None:
            self.cancelled = False

        def __enter__(self) -> t.Self:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: types.TracebackType | None,
        ) -> bool:
            _ = (exc_type, exc, traceback)
            return False

        def cancel_process(self) -> None:
            nonlocal cancellations
            cancellations += 1
            module.__dict__["cancellations"] = cancellations
            self.cancelled = True

    class FakeConversationWithTokenBudget(BaseFakeConversation):
        def send_message_async(
            self,
            message: str,
            *,
            max_output_tokens: int | None = None,
        ) -> t.Iterator[dict[str, object]]:
            assert max_output_tokens == 256
            prompts.append(message)
            return iter(chunks)

    class FakeConversationWithoutTokenBudget(BaseFakeConversation):
        def send_message_async(
            self,
            message: str,
        ) -> t.Iterator[dict[str, t.Any]]:
            nonlocal drains_after_cancel
            prompts.append(message)
            for chunk in chunks:
                if self.cancelled:
                    drains_after_cancel += 1
                    module.__dict__["drains_after_cancel"] = drains_after_cancel
                    return
                yield chunk

    class Engine:
        def __init__(
            self,
            model_path: str,
            *,
            backend: CPU,
            max_num_tokens: int,
        ) -> None:
            engines.append(
                {
                    "model_path": model_path,
                    "backend": backend.get_name(),
                    "max_num_tokens": max_num_tokens,
                },
            )

        def __enter__(self) -> t.Self:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: types.TracebackType | None,
        ) -> bool:
            _ = (exc_type, exc, traceback)
            return False

        def create_conversation(
            self,
            *,
            system_message: str,
            sampler_config: SamplerConfig,
        ) -> BaseFakeConversation:
            assert system_message == "Summarize local aggregate agentgrep report facts."
            assert sampler_config.temperature == 0.0
            if supports_max_output_tokens:
                return FakeConversationWithTokenBudget()
            return FakeConversationWithoutTokenBudget()

    module.__dict__.update(
        {
            "Backend": types.SimpleNamespace(CPU=CPU),
            "Engine": Engine,
            "SamplerConfig": SamplerConfig,
            "cancellations": cancellations,
            "drains_after_cancel": drains_after_cancel,
            "engines": engines,
            "prompts": prompts,
        },
    )
    return module


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
