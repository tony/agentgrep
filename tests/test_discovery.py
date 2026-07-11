"""Tests for scope-aware source discovery.

Search scope governs *coverage* as well as store role. These tests pin both
gates: the conversation/all opt-in must reach the ``INSPECTABLE`` tier (including
the app-state stores on the conversation allowlist), and it must not drag the
``CATALOG_ONLY`` rows — config files, shell snapshots, debug logs — into search
along with it.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import typing as t

import pytest

import agentgrep
from agentgrep import events as ag_events
from agentgrep._engine.orchestration import discover_sources_for_search, searchable_sources
from agentgrep.discovery import (
    _cursor_ide_workspace_root,
    descriptor_admits_store_roles,
    discover_sources,
)
from agentgrep.origin import PRUNABLE_ORIGIN_FIELDS
from agentgrep.query import compile_query, default_registry, parse_query
from agentgrep.records import (
    CONVERSATION_CONTENT_STORES,
    CONVERSATION_STORE_ROLES,
    PROMPT_HISTORY_STORE_ROLES,
    DiscoveryStoreRoles,
)
from agentgrep.store_catalog import CATALOG
from agentgrep.stores import SEARCHABLE_COVERAGE, StoreCoverage

NO_BACKENDS = agentgrep.BackendSelection(find_tool=None, grep_tool=None, json_tool=None)


def _codex_home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Seed a Codex home spanning all three searchable coverage tiers.

    ``history.jsonl`` is ``DEFAULT_SEARCH``, ``state_5.sqlite`` is the
    ``INSPECTABLE`` app-state store on the conversation allowlist, and
    ``config.toml`` plus ``shell_snapshots/*.sh`` are ``CATALOG_ONLY`` rows that
    carry live discovery specs — exactly the rows an inventory walk admits.
    """
    home = tmp_path / "home"
    root = home / ".codex"
    (root / "shell_snapshots").mkdir(parents=True)
    (root / "history.jsonl").write_text('{"text": "bliss"}\n', encoding="utf-8")
    (root / "state_5.sqlite").write_bytes(b"")
    (root / "config.toml").write_text('model = "gpt-5.4"\n', encoding="utf-8")
    (root / "shell_snapshots" / "snapshot-zsh.sh").write_text("export SECRET=1\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CODEX_SQLITE_HOME", raising=False)
    return home


def _query(scope: agentgrep.SearchScope) -> agentgrep.SearchQuery:
    """Build a Codex-only search query at one scope."""
    return agentgrep.SearchQuery(
        terms=("bliss",),
        scope=scope,
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=10,
    )


class RoleAdmissionCase(t.NamedTuple):
    """One catalogue row, one role narrowing, and the expected admission."""

    test_id: str
    store_id: str
    store_roles: DiscoveryStoreRoles
    admitted: bool


ROLE_ADMISSION_CASES: tuple[RoleAdmissionCase, ...] = (
    RoleAdmissionCase(
        test_id="no-narrowing-admits-everything",
        store_id="codex.config",
        store_roles=None,
        admitted=True,
    ),
    RoleAdmissionCase(
        test_id="chat-role-admits-chat-row",
        store_id="codex.sessions",
        store_roles=CONVERSATION_STORE_ROLES,
        admitted=True,
    ),
    RoleAdmissionCase(
        test_id="chat-roles-admit-allowlisted-app-state",
        store_id="codex.state_db",
        store_roles=CONVERSATION_STORE_ROLES,
        admitted=True,
    ),
    RoleAdmissionCase(
        test_id="chat-roles-reject-other-app-state",
        store_id="codex.logs_db",
        store_roles=CONVERSATION_STORE_ROLES,
        admitted=False,
    ),
    RoleAdmissionCase(
        test_id="prompt-roles-reject-allowlisted-app-state",
        store_id="codex.state_db",
        store_roles=PROMPT_HISTORY_STORE_ROLES,
        admitted=False,
    ),
)


@pytest.mark.parametrize(
    RoleAdmissionCase._fields,
    ROLE_ADMISSION_CASES,
    ids=[case.test_id for case in ROLE_ADMISSION_CASES],
)
def test_descriptor_admits_store_roles(
    test_id: str,
    store_id: str,
    store_roles: DiscoveryStoreRoles,
    admitted: bool,
) -> None:
    """Role narrowing runs before the filesystem walk and must stay coarse.

    The conversation roles have to admit the allowlisted app-state rows or their
    records are unreachable at every scope. They must not admit app-state rows
    that are *not* on the allowlist, and the prompt scope must not admit the
    allowlist at all.
    """
    descriptor = CATALOG.by_id(store_id)

    assert descriptor_admits_store_roles(descriptor, store_roles) is admitted, test_id


def test_conversation_scope_reaches_allowlisted_app_state_store(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--scope conversations`` must reach ``codex.state_db``.

    The store is ``APP_STATE`` and ``INSPECTABLE``, so both discovery gates
    (role narrowing and the coverage gate) used to exclude it. Any field its
    adapter populates would then be invisible at every scope a user can ask for.
    """
    home = _codex_home(tmp_path, monkeypatch)

    sources = discover_sources_for_search(home, _query("conversations"), NO_BACKENDS)
    stores = {source.store for source in sources}

    assert CONVERSATION_CONTENT_STORES & stores == {"codex.state_db"}


def test_catalog_only_stores_stay_out_of_every_scope(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifting the coverage gate must not drag ``CATALOG_ONLY`` rows into search.

    ``include_non_default=True`` is an inventory flag: it admits every
    ``CATALOG_ONLY`` row that carries a discovery spec, and the catalogue has
    plenty (config files, shell snapshots, debug logs). Search re-narrows to
    :data:`~agentgrep.stores.SEARCHABLE_COVERAGE`; without that, ``--scope all``
    would start returning the user's shell snapshots.
    """
    home = _codex_home(tmp_path, monkeypatch)
    inventory = {
        source.store
        for source in discover_sources(home, ("codex",), NO_BACKENDS, include_non_default=True)
    }
    assert {"codex.config", "codex.shell_snapshots"} <= inventory

    for scope in t.get_args(agentgrep.SearchScope):
        sources = discover_sources_for_search(
            home,
            _query(t.cast("agentgrep.SearchScope", scope)),
            NO_BACKENDS,
        )
        stores = {source.store for source in sources}

        assert "codex.config" not in stores, scope
        assert "codex.shell_snapshots" not in stores, scope
        assert all(source.coverage in SEARCHABLE_COVERAGE for source in sources), scope


def test_searchable_sources_drops_catalog_only_handles() -> None:
    """The coverage re-narrow is keyed on the handle, not on the store name."""
    handles = [
        agentgrep.SourceHandle(
            agent="codex",
            store=store,
            adapter_id="codex.state_sqlite.v1",
            path=pathlib.Path("/tmp/codex") / store,
            path_kind="sqlite_db",
            source_kind="sqlite",
            search_root=None,
            mtime_ns=0,
            coverage=coverage,
        )
        for store, coverage in (
            ("codex.history", StoreCoverage.DEFAULT_SEARCH),
            ("codex.state_db", StoreCoverage.INSPECTABLE),
            ("codex.logs_db", StoreCoverage.CATALOG_ONLY),
        )
    ]

    kept = {source.store for source in searchable_sources(handles)}

    assert kept == {"codex.history", "codex.state_db"}


_WORKSPACE_DIGEST = "9b2a1f0c4d3e5a6b7c8d9e0f1a2b3c4d"
"""A Cursor ``workspaceStorage`` directory name: md5 of the workspace path."""


def _cursor_workspace_home(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> pathlib.Path:
    """Seed one Cursor workspace whose record disagrees with its ``workspace.json``.

    ``workspace.json`` points the workspace at ``/work/folder``, while the
    ``composerData`` bubble inside the database carries its own ``cwd`` of
    ``/work/bubble``. Real workspaces do this: a chat can run against a
    worktree or a subdirectory that is not the folder the window was opened on.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    workspace = _cursor_ide_workspace_root(home) / _WORKSPACE_DIGEST
    workspace.mkdir(parents=True)
    _ = (workspace / "workspace.json").write_text(
        json.dumps({"folder": "file:///work/folder"}),
        encoding="utf-8",
    )
    connection = sqlite3.connect(workspace / "state.vscdb")
    try:
        _ = connection.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
        _ = connection.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            (
                "workbench.panel.chat.composerData",
                json.dumps(
                    {
                        "conversation": [
                            {"role": "user", "text": "bliss", "cwd": "/work/bubble"},
                        ],
                    },
                ),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return home


def _workspace_source(home: pathlib.Path) -> agentgrep.SourceHandle:
    """Return the discovered per-workspace Cursor state source."""
    sources = discover_sources(home, ("cursor-ide",), NO_BACKENDS)
    workspace_sources = [s for s in sources if s.store == "cursor-ide.workspace_state"]

    assert len(workspace_sources) == 1
    return workspace_sources[0]


def test_source_origin_summary_never_claims_cwd_completeness(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source may only claim completeness for values its records cannot contradict.

    ``complete_fields`` is a claim about *values*, not names: the planner drops
    a whole source when its summary says the field is complete and no listed
    value matches. So the invariant is not "the summary populated ``cwd``" — it
    is "no record carries a ``cwd`` the summary did not list". ``workspace.json``
    cannot satisfy that for ``cwd``, because the parser reads a ``cwd`` out of
    the payload, so ``cwd`` is not prunable.
    """
    home = _cursor_workspace_home(tmp_path, monkeypatch)
    source = _workspace_source(home)
    summary = source.origin_summary

    assert summary is not None
    assert summary.complete_fields <= PRUNABLE_ORIGIN_FIELDS
    assert summary.origins == (
        agentgrep.RecordOrigin(cwd="/work/folder", cwd_hash=_WORKSPACE_DIGEST),
    )

    for field in summary.complete_fields:
        claimed = {
            value
            for source_origin in summary.origins
            for value in (getattr(source_origin, field),)
            if value
        }
        for record in agentgrep.iter_source_records(source):
            assert record.origin is not None
            value = t.cast("str | None", getattr(record.origin, field))
            assert value is None or value in claimed, (field, value)


def test_source_claiming_a_cwd_does_not_prune_its_own_record(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cwd:`` must reach a record whose ``cwd`` differs from the workspace folder.

    The source-level prune runs before the file is opened. A summary that claims
    ``cwd`` completeness from ``workspace.json`` alone therefore answers
    ``cwd:/work/bubble`` with "this source cannot match" — and the record that
    does match is deleted, silently, with a successful exit code.
    """
    home = _cursor_workspace_home(tmp_path, monkeypatch)
    registry = default_registry()
    compiled = compile_query(parse_query("cwd:/work/bubble bliss", registry), registry)
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("cursor-ide",),
        limit=10,
        compiled=compiled,
    )

    records = [
        event.record
        for event in agentgrep.iter_search_events(home, query)
        if isinstance(event, ag_events.RecordEmitted)
    ]

    assert [record.text for record in records] == ["bliss"]
