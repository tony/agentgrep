"""Tests for the similarity scorer and the CLI ``similar`` verb."""

from __future__ import annotations

import json
import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep.identity import record_content_id
from agentgrep.ranking import score_by_similarity
from agentgrep.records import SearchRecord


def _record(**overrides: object) -> SearchRecord:
    """Build a SearchRecord with defaults for similarity tests."""
    fields: dict[str, object] = {
        "kind": "prompt",
        "agent": "codex",
        "store": "codex.sessions",
        "adapter_id": "codex.sessions_jsonl.v1",
        "path": pathlib.Path.home() / ".codex/sessions/r.jsonl",
        "text": "seed text",
        "role": "user",
        "timestamp": "2026-01-01T00:00:00Z",
        "session_id": "s1",
    }
    fields.update(overrides)
    return SearchRecord(**t.cast("t.Any", fields))


def test_score_ranks_best_first() -> None:
    """The scorer ranks the closest text first, normalized to 0..1."""
    seed = "how do I refactor the parser module"
    records = [
        _record(text="what is the capital of france", session_id="a"),
        _record(text="how do I refactor the parser cleanly", session_id="b"),
        _record(text=seed, session_id="c"),
    ]
    scored = score_by_similarity(seed, records)
    assert round(scored[0][1], 2) == 1.0
    assert scored[0][0].text == seed
    assert scored[-1][0].text == "what is the capital of france"


def test_verbatim_match_retained_by_default() -> None:
    """A byte-identical text is kept (the 'where else did I ask this?' answer)."""
    seed = "identical prompt"
    scored = score_by_similarity(seed, [_record(text=seed)])
    assert len(scored) == 1
    assert scored[0][1] == 1.0


def test_seed_identity_exclusion_drops_only_the_seed_record() -> None:
    """Excluding by content id drops the seed but keeps a verbatim twin elsewhere."""
    seed = "shared prompt across agents"
    origin = _record(text=seed, store="codex.sessions", adapter_id="codex.sessions_jsonl.v1")
    twin = _record(text=seed, store="claude.history", adapter_id="claude.history_jsonl.v1")
    assert record_content_id(origin) != record_content_id(twin)
    scored = score_by_similarity(
        seed,
        [origin, twin],
        seed_content_id=record_content_id(origin),
    )
    kept = [record for record, _ in scored]
    assert origin not in kept
    assert twin in kept


def test_threshold_prunes_weak_matches() -> None:
    """``threshold`` drops records below the similarity floor."""
    seed = "how do I refactor the parser module"
    records = [
        _record(text=seed, session_id="a"),
        _record(text="totally unrelated content here", session_id="b"),
    ]
    scored = score_by_similarity(seed, records, threshold=0.9)
    assert len(scored) == 1
    assert scored[0][0].text == seed


def test_top_k_caps_results() -> None:
    """``top_k`` bounds the number of neighbors returned."""
    seed = "prompt"
    records = [_record(text=f"prompt {i}", session_id=str(i)) for i in range(10)]
    assert len(score_by_similarity(seed, records, top_k=3)) == 3


def test_score_is_deterministic_on_ties() -> None:
    """Equal-scoring records sort in a stable, deterministic order."""
    seed = "same"
    records = [
        _record(text="same", agent="codex", session_id="z"),
        _record(text="same", agent="claude", session_id="a"),
    ]
    first = [record.agent for record, _ in score_by_similarity(seed, records)]
    second = [record.agent for record, _ in score_by_similarity(seed, list(reversed(records)))]
    assert first == second


def test_similar_cli_ranks_over_a_store(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI similar verb ranks records over a fixtured store."""
    monkeypatch.setenv("HOME", str(tmp_path))
    session = tmp_path / ".codex" / "sessions" / "2026" / "01" / "01" / "r.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        "\n".join(
            json.dumps({"type": "response_item", "payload": {"role": "user", "content": text}})
            for text in ("refactor the parser module", "what is the capital of france")
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = agentgrep.main(
        [
            "similar",
            "--similar-text",
            "refactor the parser module",
            "--agent",
            "codex",
            "--json",
        ],
    )
    assert exit_code == 0
    results = json.loads(capsys.readouterr().out)["results"]
    assert results[0]["text"] == "refactor the parser module"
    assert results[0]["score"] == 1.0
    assert results[0]["score"] > results[-1]["score"]


def test_running_cutoff_matches_a_full_scan() -> None:
    """The pruning cutoff returns the identical top-k as scoring every record."""
    import difflib

    seed = "how do I refactor the parser module cleanly"
    records = [
        _record(
            text=f"{'refactor parser bits' if i % 2 else 'unrelated topic'} {i}", session_id=str(i)
        )
        for i in range(30)
    ]
    got = score_by_similarity(seed, records, top_k=5)

    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(seed)
    reference: list[tuple[SearchRecord, float]] = []
    for record in records:
        matcher.set_seq1(record.text)
        reference.append((record, matcher.ratio()))
    reference.sort(
        key=lambda pair: (-pair[1], pair[0].timestamp or "", pair[0].agent, pair[0].text)
    )

    assert [(r.text, round(s, 9)) for r, s in got] == [
        (r.text, round(s, 9)) for r, s in reference[:5]
    ]


def test_similar_exclude_exact_flag(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--exclude-exact drops the verbatim seed match that the default keeps."""
    monkeypatch.setenv("HOME", str(tmp_path))
    session = tmp_path / ".codex" / "sessions" / "2026" / "01" / "01" / "r.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        "\n".join(
            json.dumps({"type": "response_item", "payload": {"role": "user", "content": text}})
            for text in ("refactor the parser module", "refactor the parser cleanly")
        )
        + "\n",
        encoding="utf-8",
    )
    seed = ["--similar-text", "refactor the parser module", "--agent", "codex", "--json"]

    assert agentgrep.main(["similar", *seed]) == 0
    kept = json.loads(capsys.readouterr().out)["results"]
    assert any(r["text"] == "refactor the parser module" and r["score"] == 1.0 for r in kept)

    assert agentgrep.main(["similar", *seed, "--exclude-exact"]) == 0
    excluded = json.loads(capsys.readouterr().out)["results"]
    assert all(r["text"] != "refactor the parser module" for r in excluded)
