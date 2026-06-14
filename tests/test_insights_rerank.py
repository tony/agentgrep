"""Tests for the archetype-cluster rerank ladder (cross-encoder / TF-IDF)."""

from __future__ import annotations

import types
import typing as t

import pytest

from agentgrep.insights.enrichers import rerank


def _importer(modules: dict[str, t.Any]) -> t.Callable[[str], t.Any]:
    """Return a fake importer resolving only the given modules."""

    def _imp(name: str) -> t.Any:
        if name in modules:
            return modules[name]
        raise ImportError(name)

    return _imp


def test_rerank_none_tier_when_no_backend_available() -> None:
    """With neither sentence-transformers nor sklearn, clustering is unchanged."""
    clusters = [[0, 1, 2]]
    texts = ["a", "b", "c"]
    out, tier = rerank.rerank_clusters(clusters, texts, import_module=_importer({}))
    assert tier == "none"
    assert out == clusters


def _sklearn_only_importer() -> t.Callable[[str], t.Any]:
    """Real importer that blocks sentence-transformers so the TF-IDF tier runs.

    sentence-transformers may be installed in the dev env; forcing it absent
    makes the rerank ladder fall through the cross-encoder tier to TF-IDF.
    """
    import importlib

    def _imp(name: str) -> t.Any:
        if name == "sentence_transformers":
            raise ImportError(name)
        return importlib.import_module(name)

    return _imp


def test_rerank_tfidf_splits_lexical_contaminant() -> None:
    """The TF-IDF gate demotes a member with no token overlap with the core."""
    pytest.importorskip("sklearn.feature_extraction.text")
    texts = [
        "commit keybindings json file",
        "add keybindings json and commit",
        "keybindings json commit now",
        "save the keybindings json then commit",
        "unrelated postgres database migration rollback",
    ]
    out, tier = rerank.rerank_clusters(
        [[0, 1, 2, 3, 4]], texts, import_module=_sklearn_only_importer()
    )
    assert tier == "tfidf"
    assert [0, 1, 2, 3] in out  # the keybinding asks stay together
    assert [4] in out  # the unrelated migration ask is demoted


def test_rerank_tfidf_keeps_cohesive_cluster() -> None:
    """A genuinely cohesive cluster survives the gate intact."""
    pytest.importorskip("sklearn.feature_extraction.text")
    texts = ["commit the keybindings json", "commit the keybindings json now"]
    out, tier = rerank.rerank_clusters([[0, 1]], texts, import_module=_sklearn_only_importer())
    assert tier == "tfidf"
    assert [0, 1] in out


def _fake_sentence_transformers(score_for: t.Callable[[str, str], float]) -> t.Any:
    """Fake sentence_transformers whose CrossEncoder scores via ``score_for``."""

    class _CrossEncoder:
        def __init__(self, _path: str) -> None:
            pass

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [score_for(a, b) for a, b in pairs]

    return types.SimpleNamespace(CrossEncoder=_CrossEncoder)


def test_rerank_cross_encoder_splits_low_scored_member(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cross-encoder demotes a member weakly related to the cohesive core."""
    from agentgrep.insights import models as models_mod

    # Pretend the curated reranker is already provisioned so it loads.
    monkeypatch.setattr(models_mod, "is_installed", lambda spec, cache=None: True)

    # The duplicate-question cross-encoder returns a 0-1 probability (sigmoid
    # applied internally), used directly by the keep-rule.
    def score(a: str, b: str) -> float:
        return 0.02 if ("tdd" in a or "tdd" in b) else 0.98

    importer = _importer({"sentence_transformers": _fake_sentence_transformers(score)})
    texts = [
        "commit keybindings A",
        "commit keybindings B",
        "commit keybindings C",
        "fix the tdd assurances",
    ]
    out, tier = rerank.rerank_clusters([[0, 1, 2, 3]], texts, import_module=importer)
    assert tier == "cross-encoder"
    assert [0, 1, 2] in out  # keybinding core kept together
    assert [3] in out  # tdd contaminant demoted


def test_rerank_cross_encoder_skipped_when_model_not_provisioned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a provisioned model and no download permission, fall to TF-IDF/none."""
    from agentgrep.insights import models as models_mod

    monkeypatch.setattr(models_mod, "is_installed", lambda spec, cache=None: False)
    importer = _importer({"sentence_transformers": _fake_sentence_transformers(lambda a, b: 0.0)})
    out, tier = rerank.rerank_clusters(
        [[0, 1]], ["x", "y"], import_module=importer, allow_download=False
    )
    # sentence-transformers present but model absent -> not cross-encoder; sklearn
    # absent in this importer -> none.
    assert tier == "none"
    assert out == [[0, 1]]
