"""Tests for the curated model registry and the artifact downloader."""

from __future__ import annotations

import email.message
import pathlib
import types
import typing as t
import urllib.error

import pytest

from agentgrep.insights import models as models_mod
from agentgrep.insights.loader import BackendConfigurationError

if t.TYPE_CHECKING:
    from agentgrep.insights.models import EmbeddingModelSpec, LLMModelSpec


def _embedding_spec(model_id: str) -> EmbeddingModelSpec:
    """Resolve an embedding spec, asserting it exists (narrows the optional)."""
    spec = models_mod.resolve_embedding_model(model_id)
    assert spec is not None
    return spec


def _llm_spec(model_id: str, backend: str) -> LLMModelSpec:
    """Resolve an LLM spec, asserting it exists (narrows the optional)."""
    spec = models_mod.resolve_llm_model(model_id, backend)
    assert spec is not None
    return spec


def test_registry_lists_and_resolves() -> None:
    """The static registry lists curated models without network access."""
    embeds = models_mod.list_embedding_models()
    assert {spec.model_id for spec in embeds} >= {"all-MiniLM-L6-v2", "potion-base-8M"}
    assert _embedding_spec("potion-base-8M").runtime == "model2vec"
    assert _embedding_spec("minilm-l6-v2").model_id == "all-MiniLM-L6-v2"
    assert _llm_spec("llama3.2", "ollama").backend == "ollama"
    assert models_mod.resolve_embedding_model("nope") is None


def test_cache_path_layout(tmp_path: pathlib.Path) -> None:
    """Cache paths are scoped by kind/backend/local_id."""
    path = models_mod.model_cache_path(_embedding_spec("potion-base-8M"), tmp_path)
    assert path == tmp_path / "embeddings" / "model2vec" / "potion-base-8m"


class _FakeResponse:
    """Minimal urlopen response stand-in serving fixed bytes in chunks."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self, size: int) -> bytes:
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk


def test_install_via_urllib_writes_files_and_manifest(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model2vec spec downloads each file via urllib and writes a manifest."""

    def fake_urlopen(request: t.Any) -> _FakeResponse:
        return _FakeResponse(b"bytes-for-" + request.full_url.encode()[-12:])

    monkeypatch.setattr(models_mod.urllib.request, "urlopen", fake_urlopen)
    spec = _embedding_spec("potion-base-8M")

    result = models_mod.install_model(spec, model_cache=tmp_path)

    assert result.cached is False
    assert result.bytes_downloaded > 0
    assert set(result.files) == {"config.json", "model.safetensors", "tokenizer.json"}
    target = models_mod.model_cache_path(spec, tmp_path)
    assert (target / "model.safetensors").is_file()
    assert (target / "agentgrep-manifest.json").is_file()
    assert models_mod.is_installed(spec, tmp_path) is True


def test_install_is_cached_on_second_call(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second install of an already-provisioned model is a no-op."""
    monkeypatch.setattr(models_mod.urllib.request, "urlopen", lambda request: _FakeResponse(b"x"))
    spec = _embedding_spec("potion-base-8M")
    models_mod.install_model(spec, model_cache=tmp_path)

    second = models_mod.install_model(spec, model_cache=tmp_path)
    assert second.cached is True
    assert second.bytes_downloaded == 0


def test_install_dry_run_writes_nothing(tmp_path: pathlib.Path) -> None:
    """A dry-run reports the plan without touching the filesystem."""
    spec = _embedding_spec("potion-base-8M")
    result = models_mod.install_model(spec, model_cache=tmp_path, dry_run=True)
    assert result.dry_run is True
    assert not models_mod.model_cache_path(spec, tmp_path).exists()


def test_install_snapshot_path_for_multifile_repo(tmp_path: pathlib.Path) -> None:
    """A files-less spec (all-MiniLM) downloads via huggingface_hub.snapshot_download."""

    def snapshot_download(
        *, repo_id: str, revision: str, local_dir: str, token: str | None
    ) -> None:
        directory = pathlib.Path(local_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "config.json").write_text("{}", encoding="utf-8")
        (directory / "model.safetensors").write_bytes(b"weights")

    fake_hf = types.SimpleNamespace(snapshot_download=snapshot_download)

    def importer(name: str) -> t.Any:
        if name == "huggingface_hub":
            return fake_hf
        message = name
        raise ImportError(message)

    spec = _embedding_spec("all-MiniLM-L6-v2")
    result = models_mod.install_model(spec, model_cache=tmp_path, import_module=importer)

    assert "model.safetensors" in result.files
    assert models_mod.is_installed(spec, tmp_path) is True


def test_install_ollama_model_is_not_downloaded(tmp_path: pathlib.Path) -> None:
    """Ollama-managed models raise with the ``ollama pull`` instruction."""
    spec = _llm_spec("llama3.2", "ollama")
    with pytest.raises(BackendConfigurationError, match="ollama pull"):
        models_mod.install_model(spec, model_cache=tmp_path)


def test_gated_download_raises_configuration_error(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401/403 download surfaces a typed configuration error mentioning HF_TOKEN."""

    def fake_urlopen(request: t.Any) -> _FakeResponse:
        raise urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", email.message.Message(), None
        )

    monkeypatch.setattr(models_mod.urllib.request, "urlopen", fake_urlopen)
    spec = _embedding_spec("potion-base-8M")
    with pytest.raises(BackendConfigurationError, match="HF_TOKEN"):
        models_mod.install_model(spec, model_cache=tmp_path)
