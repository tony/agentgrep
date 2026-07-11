"""Tests for scope-aware source discovery.

Search scope governs *coverage* as well as store role. These tests pin both
gates: the conversation/all opt-in must reach the ``INSPECTABLE`` tier (including
the app-state stores on the conversation allowlist), and it must not drag the
``CATALOG_ONLY`` rows — config files, shell snapshots, debug logs — into search
along with it.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep._engine.orchestration import discover_sources_for_search, searchable_sources
from agentgrep.discovery import descriptor_admits_store_roles, discover_sources
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
