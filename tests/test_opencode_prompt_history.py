"""OpenCode prompt-history store: parsing and discovery contracts."""

from __future__ import annotations

import pathlib
import shutil
import sqlite3
import typing as t

import pytest

from agentgrep.adapters.opencode import parse_opencode_prompt_history
from agentgrep.discovery import discover_opencode_sources
from agentgrep.records import PROMPT_HISTORY_STORE_ROLES, BackendSelection, SourceHandle
from agentgrep.stores import StoreRole

from .conftest import fixture_path

_NO_BACKENDS = BackendSelection(find_tool=None, grep_tool=None, json_tool=None)


def test_prompt_history_splices_pastes_and_invents_no_date() -> None:
    """Each line is one undated, sessionless prompt with its pastes restored."""
    source = SourceHandle(
        agent="opencode",
        store="opencode.prompt_history",
        adapter_id="opencode.prompt_history_jsonl.v1",
        path=fixture_path("opencode.prompt_history", "prompt-history.jsonl"),
        path_kind="history_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=0,
    )
    records = list(parse_opencode_prompt_history(source))
    assert [record.text for record in records] == [
        "Summarize the parser module",
        "Review def parse(text):\n    return tokens(text) before merging",
    ]
    assert {(record.kind, record.role) for record in records} == {("prompt", "user")}
    assert {(record.timestamp, record.session_id) for record in records} == {(None, None)}
    assert records[0].metadata == {"mode": "normal"}


class DiscoveryCase(t.NamedTuple):
    """One environment shape and the OpenCode stores discovery should find."""

    test_id: str
    relocate_db: bool
    store_roles: frozenset[StoreRole] | None
    expected_stores: tuple[str, ...]


DISCOVERY_CASES = (
    DiscoveryCase("prompt-effort", False, PROMPT_HISTORY_STORE_ROLES, ("opencode.prompt_history",)),
    DiscoveryCase(
        "prompt-effort-relocated-db",
        True,
        PROMPT_HISTORY_STORE_ROLES,
        ("opencode.prompt_history",),
    ),
    DiscoveryCase(
        "every-role-relocated-db",
        True,
        None,
        ("opencode.db", "opencode.prompt_history"),
    ),
)


@pytest.mark.parametrize(
    list(DiscoveryCase._fields),
    DISCOVERY_CASES,
    ids=[case.test_id for case in DISCOVERY_CASES],
)
def test_prompt_history_is_found_from_the_state_root(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    test_id: str,
    relocate_db: bool,
    store_roles: frozenset[StoreRole] | None,
    expected_stores: tuple[str, ...],
) -> None:
    """The state root is searched whether or not ``OPENCODE_DB`` moves the database."""
    home = tmp_path / "home"
    state = tmp_path / "state"
    (state / "opencode").mkdir(parents=True)
    shutil.copy(
        fixture_path("opencode.prompt_history", "prompt-history.jsonl"),
        state / "opencode" / "prompt-history.jsonl",
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    if relocate_db:
        database = tmp_path / "elsewhere" / "opencode-dev.db"
        database.parent.mkdir()
        sqlite3.connect(database).close()
        monkeypatch.setenv("OPENCODE_DB", str(database))
    handles = discover_opencode_sources(
        home,
        _NO_BACKENDS,
        version_detail="catalog",
        store_roles=store_roles,
    )
    assert tuple(sorted(handle.store for handle in handles)) == expected_stores
