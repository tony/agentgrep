"""Tests for the frontend-neutral export core and the CLI ``export`` verb."""

from __future__ import annotations

import io
import json
import pathlib
import sys
import typing as t

import pytest

import agentgrep
from agentgrep import export
from agentgrep.records import SearchRecord


def _record(**overrides: object) -> SearchRecord:
    """Build a SearchRecord with defaults for export tests."""
    fields: dict[str, object] = {
        "kind": "prompt",
        "agent": "codex",
        "store": "codex.sessions",
        "adapter_id": "codex.sessions_jsonl.v1",
        "path": pathlib.Path.home() / ".codex/sessions/rollout.jsonl",
        "text": "export me",
        "role": "user",
        "timestamp": "2026-01-01T00:00:00Z",
        "session_id": "s1",
    }
    fields.update(overrides)
    return SearchRecord(**t.cast("t.Any", fields))


def test_ndjson_is_byte_identical_across_runs() -> None:
    """The export total order makes NDJSON reruns byte-identical (diffable)."""
    records = [
        _record(text="beta", timestamp="2026-01-02T00:00:00Z"),
        _record(text="alpha", timestamp="2026-01-01T00:00:00Z"),
    ]
    first = "\n".join(export.iter_ndjson_lines(records))
    second = "\n".join(export.iter_ndjson_lines(list(reversed(records))))
    assert first == second
    # sorted by (timestamp, content-id): "alpha" (earlier ts) leads.
    assert json.loads(first.splitlines()[0])["text"] == "alpha"


def test_ndjson_carries_content_id_without_pydantic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Export reuses the pydantic-free serializer, so it emits ids with no pydantic."""
    monkeypatch.setitem(sys.modules, "pydantic", None)
    with pytest.raises(ImportError):
        import pydantic  # noqa: F401
    line = next(iter(export.iter_ndjson_lines([_record()])))
    payload = json.loads(line)
    assert payload["content_id"]
    assert payload["id"]


def test_markdown_fence_bumps_on_nested_backticks() -> None:
    """A turn body containing a triple backtick gets a longer fence."""
    conversations = export.assemble_conversations([_record(text="a ``` fence", role="assistant")])
    rendered = export.render_markdown(conversations)
    assert "````\na ``` fence\n````" in rendered
    assert rendered.startswith("---\n")  # YAML front-matter


def test_redact_record_replaces_body_keeps_shape() -> None:
    """Redaction swaps the body for a stable hash but keeps ids and provenance."""
    from agentgrep.cli.serializers import serialize_search_record

    payload = dict(serialize_search_record(_record(text="secret prompt")))
    redacted = export.redact_record(payload)
    redacted_text = redacted["text"]
    assert isinstance(redacted_text, str)
    assert redacted_text.startswith("sha256:")
    assert "secret prompt" not in redacted_text
    assert redacted["content_id"] == payload["content_id"]
    assert redacted["agent"] == "codex"
    # never leak an absolute home path
    assert str(pathlib.Path.home()) not in json.dumps(redacted)


def test_assemble_groups_by_session_and_orders_turns() -> None:
    """Records group by the session-identity ladder; turns order by total key."""
    records = [
        _record(session_id="s1", text="second", timestamp="2026-01-01T00:00:02Z"),
        _record(session_id="s1", text="first", timestamp="2026-01-01T00:00:01Z"),
        _record(session_id="s2", text="other", timestamp="2026-01-01T00:00:03Z"),
    ]
    conversations = export.assemble_conversations(records)
    assert len(conversations) == 2
    s1 = next(conv for conv in conversations if conv.id == "s1")
    assert [turn.text for turn in s1.turns] == ["first", "second"]


def test_csv_quotes_embedded_delimiters() -> None:
    """Embedded commas/newlines survive CSV via csv.writer, not a hand-rolled join."""
    rendered = export.render_csv([_record(text="a, b\nc")])
    rows = list(__import__("csv").reader(io.StringIO(rendered)))
    assert rows[0] == list(export._CSV_COLUMNS)
    assert rows[1][-1] == "a, b\nc"


def test_export_cli_roundtrip(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI export verb emits ndjson and markdown over a fixtured store."""
    monkeypatch.setenv("HOME", str(tmp_path))
    session = tmp_path / ".codex" / "sessions" / "2026" / "01" / "01" / "rollout.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps({"type": "response_item", "payload": {"role": "user", "content": "export cli"}})
        + "\n",
        encoding="utf-8",
    )

    assert agentgrep.main(["export", "cli", "--agent", "codex", "--format", "ndjson"]) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert payload["content_id"]
    assert payload["text"] == "export cli"

    assert agentgrep.main(["export", "cli", "--agent", "codex", "--format", "markdown"]) == 0
    markdown = capsys.readouterr().out
    assert markdown.startswith("---\n")
    assert "## user" in markdown
    assert "export cli" in markdown


def test_redact_scrubs_title_and_metadata() -> None:
    """Redaction hashes the title and drops path-bearing metadata, not just text."""
    from agentgrep.cli.serializers import serialize_search_record

    record = _record(
        text="secret prompt",
        title="Fix the auth bug in login",
        metadata={"directory": "/home/someone/secret-repo"},
    )
    redacted = export.redact_record(dict(serialize_search_record(record)))
    title = redacted["title"]
    assert isinstance(title, str)
    assert title.startswith("sha256:")
    assert redacted["metadata"] == {}
    dumped = json.dumps(redacted)
    assert "Fix the auth bug in login" not in dumped
    assert "/home/someone/secret-repo" not in dumped


def test_export_tolerates_lone_surrogate_body(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lone surrogate in store text exports instead of aborting the run."""
    monkeypatch.setenv("HOME", str(tmp_path))
    session = tmp_path / ".codex" / "sessions" / "2026" / "01" / "01" / "r.jsonl"
    session.parent.mkdir(parents=True)
    # ensure_ascii escapes the surrogate to \ud800; json.loads decodes it back
    # to a lone surrogate, exercising the surrogatepass write boundary.
    session.write_text(
        json.dumps(
            {"type": "response_item", "payload": {"role": "user", "content": "bad\ud800end"}}
        )
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "export.ndjson"
    exit_code = agentgrep.main(
        ["export", "--agent", "codex", "--format", "ndjson", "--out", str(out)]
    )
    assert exit_code == 0
    first = out.read_text(encoding="utf-8", errors="surrogatepass").splitlines()[0]
    assert json.loads(first)["text"] == "bad\ud800end"
