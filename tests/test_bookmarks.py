"""Tests for the bookmark store, re-resolver, and the CLI ``bookmark`` verb."""

from __future__ import annotations

import io
import json
import os
import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep import bookmarks


def _entry(**overrides: object) -> bookmarks.BookmarkEntry:
    """Build a BookmarkEntry with defaults for store tests."""
    fields: dict[str, object] = {
        "id": "a" * 64,
        "agent": "codex",
        "store": "sessions",
        "adapter_id": "codex.sessions_jsonl.v1",
        "path": "~/.codex/x.jsonl",
        "snippet": "hello",
    }
    fields.update(overrides)
    return bookmarks.BookmarkEntry(**t.cast("t.Any", fields))


def test_add_list_roundtrip_newest_first(tmp_path: pathlib.Path) -> None:
    """Bookmarks load newest-first by creation time."""
    path = tmp_path / "bookmarks.jsonl"
    assert bookmarks.add_bookmark(path, _entry(id="a" * 64), now=1.0)
    assert bookmarks.add_bookmark(path, _entry(id="b" * 64), now=2.0)
    assert [entry.id for entry in bookmarks.load_bookmarks(path)] == ["b" * 64, "a" * 64]


def test_add_is_idempotent_by_id(tmp_path: pathlib.Path) -> None:
    """Re-adding the same content id is a no-op, not a duplicate row."""
    path = tmp_path / "bookmarks.jsonl"
    assert bookmarks.add_bookmark(path, _entry(id="a" * 64, note="first"), now=1.0)
    assert not bookmarks.add_bookmark(path, _entry(id="a" * 64, note="second"), now=2.0)
    entries = bookmarks.load_bookmarks(path)
    assert len(entries) == 1
    assert entries[0].note == "first"


def test_remove_by_prefix(tmp_path: pathlib.Path) -> None:
    """Removing by a short-id prefix drops exactly the matched entry."""
    path = tmp_path / "bookmarks.jsonl"
    bookmarks.add_bookmark(path, _entry(id="a" * 64), now=1.0)
    bookmarks.add_bookmark(path, _entry(id="b" * 64), now=2.0)
    target = _entry(id="a" * 64)
    removed = bookmarks.remove_bookmark(path, target.short[:6])
    assert removed is not None
    assert removed.id == "a" * 64
    assert [entry.id for entry in bookmarks.load_bookmarks(path)] == ["b" * 64]


def test_find_by_prefix_unique_ambiguous_and_miss(tmp_path: pathlib.Path) -> None:
    """Prefix resolution reports unique / ambiguous / miss, git-style."""
    entries = [_entry(id="a" * 64), _entry(id="b" * 64)]
    unique = bookmarks.find_by_prefix(entries, entries[0].short)
    assert unique.entry is entries[0]
    ambiguous = bookmarks.find_by_prefix(entries, "")
    assert ambiguous.entry is None
    assert len(ambiguous.ambiguous) == 2
    miss = bookmarks.find_by_prefix(entries, "zzzzzz")
    assert miss.entry is None
    assert miss.ambiguous == ()


def test_bookmarks_disabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AGENTGREP_NO_BOOKMARKS`` opts the user out."""
    monkeypatch.setenv("AGENTGREP_NO_BOOKMARKS", "1")
    assert bookmarks.bookmarks_disabled()
    monkeypatch.setenv("AGENTGREP_NO_BOOKMARKS", "0")
    assert not bookmarks.bookmarks_disabled()
    monkeypatch.delenv("AGENTGREP_NO_BOOKMARKS", raising=False)
    assert not bookmarks.bookmarks_disabled()


def test_bookmarks_path_prefers_xdg_data_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """The store lives under XDG_DATA_HOME when set, else ~/.local/share."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert bookmarks.bookmarks_path(tmp_path) == tmp_path / "xdg" / "agentgrep" / "bookmarks.jsonl"
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    fallback = bookmarks.bookmarks_path(tmp_path)
    assert fallback == tmp_path / ".local" / "share" / "agentgrep" / "bookmarks.jsonl"


def _clean_bookmark_env(monkeypatch: pytest.MonkeyPatch, home: pathlib.Path) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.delenv("AGENTGREP_NO_BOOKMARKS", raising=False)


def test_bookmark_cli_roundtrip(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Add (stdin) -> list -> show -> rm round-trips through the CLI."""
    _clean_bookmark_env(monkeypatch, tmp_path)
    record = {
        "content_id": "de" * 32,
        "adapter_id": "codex.sessions_jsonl.v1",
        "agent": "codex",
        "path": "~/.codex/x.jsonl",
        "text": "refactor the parser",
        "title": "my prompt",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(record)))
    assert agentgrep.main(["bookmark", "add"]) == 0
    assert "bookmarked" in capsys.readouterr().out

    assert agentgrep.main(["bookmark", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    row = payload["results"][0]
    assert row["content_id"] == "de" * 32
    short = row["id"]

    assert agentgrep.main(["bookmark", "show", short[:4]]) == 0
    assert "my prompt" in capsys.readouterr().out

    assert agentgrep.main(["bookmark", "rm", short[:4]]) == 0
    assert "removed" in capsys.readouterr().out

    assert agentgrep.main(["bookmark", "list"]) == 0
    assert "no bookmarks" in capsys.readouterr().out


def test_bookmark_cli_disabled(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI refuses when bookmarks are opted out."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENTGREP_NO_BOOKMARKS", "1")
    assert agentgrep.main(["bookmark", "list"]) == 1


def test_bookmark_resolves_live_record_after_store_rewrite(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A pinned record re-resolves after an in-place store rewrite.

    This is the headline guarantee: the content id excludes the mtime-derived
    timestamp, so advancing the file mtime (as flat prompt stores do every
    session) does not break ``bookmark show``.
    """
    _clean_bookmark_env(monkeypatch, tmp_path)
    session = tmp_path / ".codex" / "sessions" / "2026" / "01" / "01" / "rollout-abc.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps({"type": "response_item", "payload": {"role": "user", "content": "resolve me"}})
        + "\n",
        encoding="utf-8",
    )

    # Discover the record through the real search surface (carries content_id).
    assert agentgrep.main(["search", "resolve", "--ndjson", "--agent", "codex"]) == 0
    line = capsys.readouterr().out.strip().splitlines()[0]
    record = json.loads(line)
    assert record["content_id"]

    monkeypatch.setattr("sys.stdin", io.StringIO(line))
    assert agentgrep.main(["bookmark", "add"]) == 0
    _ = capsys.readouterr()

    def _resolved_text(short: str) -> str | None:
        assert agentgrep.main(["bookmark", "show", short, "--json"]) == 0
        shown = json.loads(capsys.readouterr().out)
        return None if shown["resolved"] is None else shown["resolved"]["text"]

    assert _resolved_text(record["id"][:6]) == "resolve me"

    # Rewrite in place, advancing only the mtime; the id (and resolution) hold.
    os.utime(session, (10_000_000_000, 10_000_000_000))
    assert _resolved_text(record["id"][:6]) == "resolve me"
