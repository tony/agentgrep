"""Shape tests for the agentgrep store catalogue.

These tests assert invariants that adapter authors and catalog editors
should be able to rely on. They run on the static catalogue data; no I/O.
"""

from __future__ import annotations

import pathlib
import re
import typing as t

import pytest

from agentgrep.store_catalog import CATALOG, OBSERVED_AT, gemini_project_hash
from agentgrep.stores import (
    AgentName,
    DiscoverySpec,
    StoreCatalog,
    StoreDescriptor,
    StoreFormat,
    StoreRole,
)

KNOWN_AGENTS: tuple[AgentName, ...] = ("claude", "cursor", "codex", "gemini")
PATH_TOKEN_RE = re.compile(r"\$\{(?:HOME|[A-Z][A-Z0-9_]*)(?:\s+or\s+[^}]+)?\}")


def test_catalog_has_known_metadata() -> None:
    assert CATALOG.catalog_version >= 1
    assert CATALOG.captured_at.year >= 2026
    assert len(CATALOG.stores) > 0


def test_every_store_has_known_agent() -> None:
    for store in CATALOG.stores:
        assert store.agent in KNOWN_AGENTS, store.store_id


def test_store_ids_are_unique() -> None:
    seen: set[str] = set()
    for store in CATALOG.stores:
        assert store.store_id not in seen, f"duplicate store_id: {store.store_id}"
        seen.add(store.store_id)


def test_store_id_prefix_matches_agent() -> None:
    """``store_id`` must start with its ``agent`` prefix so it's self-describing."""
    for store in CATALOG.stores:
        assert store.store_id.startswith(f"{store.agent}."), store.store_id


def test_schema_notes_are_non_empty() -> None:
    for store in CATALOG.stores:
        assert store.schema_notes.strip(), store.store_id


def test_path_patterns_use_token_form() -> None:
    """Patterns must not leak machine-specific paths into the catalogue."""
    for store in CATALOG.stores:
        assert PATH_TOKEN_RE.search(store.path_pattern), (
            f"{store.store_id}: path_pattern '{store.path_pattern}' lacks "
            "${HOME} or ${ENV} token"
        )
        assert "/home/" not in store.path_pattern, store.store_id
        assert "/Users/" not in store.path_pattern, store.store_id


def test_primary_chat_stores_have_upstream_or_sample() -> None:
    """Stores we plan to parse need *some* schema provenance pinned."""
    for store in CATALOG.stores:
        if store.role is not StoreRole.PRIMARY_CHAT:
            continue
        assert store.upstream_ref or store.sample_record, store.store_id


def test_distinguishes_from_resolves() -> None:
    """Cross-references must point to actual stores in the catalogue."""
    valid_ids = {s.store_id for s in CATALOG.stores}
    for store in CATALOG.stores:
        for sibling in store.distinguishes_from:
            assert sibling in valid_ids, (
                f"{store.store_id}.distinguishes_from -> '{sibling}' is unknown"
            )


def test_env_overrides_are_uppercase_identifiers() -> None:
    for store in CATALOG.stores:
        for env in store.env_overrides:
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*", env), (store.store_id, env)


def test_catalog_is_frozen() -> None:
    sample = CATALOG.stores[0]
    with pytest.raises(pydantic_validation_error()):
        sample.store_id = "mutated"  # type: ignore[misc]


def pydantic_validation_error() -> type[BaseException]:
    """Return the exception pydantic raises on frozen-model mutation."""
    import pydantic

    return pydantic.ValidationError


def test_by_id_lookup() -> None:
    expected = CATALOG.stores[0]
    found = CATALOG.by_id(expected.store_id)
    assert found is expected


def test_by_id_missing_raises_key_error() -> None:
    with pytest.raises(KeyError):
        CATALOG.by_id("not.a.real.store")


def test_for_agent_filters_to_owner() -> None:
    for agent in KNOWN_AGENTS:
        subset = CATALOG.for_agent(agent)
        assert subset, f"no stores registered for {agent}"
        assert all(s.agent == agent for s in subset)


def test_each_agent_has_at_least_one_primary_or_history_store() -> None:
    """If we declared an agent at all, we declared something searchable for it."""
    for agent in KNOWN_AGENTS:
        roles = {s.role for s in CATALOG.for_agent(agent)}
        assert roles & {StoreRole.PRIMARY_CHAT, StoreRole.PROMPT_HISTORY}, agent


def test_gemini_project_hash_matches_sha256() -> None:
    import hashlib
    import pathlib

    sample = pathlib.Path("/example/repo")
    expected = hashlib.sha256(b"/example/repo").hexdigest()
    assert gemini_project_hash(sample) == expected


def test_catalog_json_schema_emits() -> None:
    """The catalog must expose a stable JSON schema (used for downstream validation)."""
    schema = StoreCatalog.model_json_schema()
    assert schema.get("title") == "StoreCatalog"


def test_descriptor_format_and_role_are_enum_members() -> None:
    valid_formats = set(StoreFormat)
    valid_roles = set(StoreRole)
    for store in CATALOG.stores:
        assert store.format in valid_formats, store.store_id
        assert store.role in valid_roles, store.store_id


def test_search_by_default_only_true_for_searchable_roles() -> None:
    """``search_by_default=True`` shouldn't appear on caches or source trees."""
    searchable = {
        StoreRole.PRIMARY_CHAT,
        StoreRole.SUPPLEMENTARY_CHAT,
        StoreRole.PROMPT_HISTORY,
        StoreRole.PERSISTENT_MEMORY,
        StoreRole.PLAN,
        StoreRole.TODO,
    }
    for store in CATALOG.stores:
        if store.search_by_default is True:
            assert store.role in searchable, (store.store_id, store.role)


def test_runtime_adapter_ids_match_catalogue_discovery() -> None:
    """Every runtime adapter id is declared by a catalogue DiscoverySpec.

    Prevents drift between the discover/dispatch path and the catalogue.
    """
    import agentgrep.mcp as agentgrep_mcp

    runtime_adapter_ids: set[str] = set()
    for store in CATALOG.stores:
        for spec in store.discovery:
            runtime_adapter_ids.add(spec.adapter_id)

    assert "claude.projects_jsonl.v1" in runtime_adapter_ids
    assert "codex.history_json.v1" in runtime_adapter_ids
    assert "codex.sessions_jsonl.v1" in runtime_adapter_ids
    assert "cursor.ai_tracking_sqlite.v1" in runtime_adapter_ids
    assert "cursor.state_vscdb_modern.v1" in runtime_adapter_ids
    assert "cursor.state_vscdb_legacy.v1" in runtime_adapter_ids
    assert "cursor.cli_jsonl.v1" in runtime_adapter_ids
    assert "gemini.tmp_chats_jsonl.v1" in runtime_adapter_ids
    assert "gemini.tmp_logs_json.v1" in runtime_adapter_ids

    # No catalogue row claims an adapter id the MCP capabilities
    # tuple doesn't advertise.
    advertised = set(agentgrep_mcp.KNOWN_ADAPTERS)
    assert runtime_adapter_ids.issubset(advertised), runtime_adapter_ids - advertised


def test_discover_from_catalog_skips_search_by_default_false(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``discover_from_catalog`` must honour the catalogue contract.

    The module docstring on :mod:`agentgrep.store_catalog` says the
    discover functions "consult" ``search_by_default`` — rows with
    ``False`` must be left untouched even if they carry a
    ``DiscoverySpec``.
    """
    import agentgrep
    import agentgrep.store_catalog as store_catalog

    base = tmp_path / "base"
    base.mkdir()
    skipped_file = base / "skipped.jsonl"
    _ = skipped_file.write_text("{}", encoding="utf-8")
    searched_file = base / "searched.jsonl"
    _ = searched_file.write_text("{}", encoding="utf-8")

    fake_catalog = StoreCatalog(
        catalog_version=999,
        captured_at=OBSERVED_AT,
        stores=(
            StoreDescriptor(
                agent="codex",
                store_id="codex.test_skipped",
                role=StoreRole.PRIMARY_CHAT,
                format=StoreFormat.JSONL,
                path_pattern="${HOME}/skipped",
                observed_version="test",
                observed_at=OBSERVED_AT,
                schema_notes="Test row that must be skipped.",
                search_by_default=False,
                discovery=(
                    DiscoverySpec(
                        store="codex.test_skipped",
                        adapter_id="codex.test_skipped.v1",
                        path_kind="session_file",
                        source_kind="jsonl",
                        files=("skipped.jsonl",),
                    ),
                ),
            ),
            StoreDescriptor(
                agent="codex",
                store_id="codex.test_searched",
                role=StoreRole.PRIMARY_CHAT,
                format=StoreFormat.JSONL,
                path_pattern="${HOME}/searched",
                observed_version="test",
                observed_at=OBSERVED_AT,
                schema_notes="Test row that must be searched.",
                search_by_default=True,
                discovery=(
                    DiscoverySpec(
                        store="codex.test_searched",
                        adapter_id="codex.test_searched.v1",
                        path_kind="session_file",
                        source_kind="jsonl",
                        files=("searched.jsonl",),
                    ),
                ),
            ),
        ),
    )
    monkeypatch.setattr(store_catalog, "CATALOG", fake_catalog)

    backends = agentgrep.BackendSelection(None, None, None)
    sources = agentgrep.discover_from_catalog(tmp_path, "codex", base, backends)

    discovered_paths = {s.path for s in sources}
    assert searched_file in discovered_paths
    assert skipped_file not in discovered_paths


def test_discover_from_catalog_deduplicates_paths_within_descriptor(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two ``DiscoverySpec``s on one descriptor yield one handle per file.

    Mirrors the Cursor IDE ``state.vscdb`` case: the modern
    ``platform_paths`` spec and the legacy home-subpath glob can both
    match a single file on non-standard layouts.
    """
    import agentgrep
    import agentgrep.store_catalog as store_catalog

    base = tmp_path / "base"
    base.mkdir()
    target = base / "state.vscdb"
    _ = target.write_text("placeholder", encoding="utf-8")

    fake_catalog = StoreCatalog(
        catalog_version=999,
        captured_at=OBSERVED_AT,
        stores=(
            StoreDescriptor(
                agent="cursor",
                store_id="cursor.test.shared",
                role=StoreRole.PRIMARY_CHAT,
                format=StoreFormat.SQLITE,
                path_pattern="${HOME}/test/shared",
                observed_version="test",
                observed_at=OBSERVED_AT,
                schema_notes="Test row with two specs hitting the same file.",
                search_by_default=True,
                discovery=(
                    DiscoverySpec(
                        store="cursor.shared",
                        adapter_id="cursor.shared_first.v1",
                        path_kind="sqlite_db",
                        source_kind="sqlite",
                        files=("state.vscdb",),
                    ),
                    DiscoverySpec(
                        store="cursor.shared",
                        adapter_id="cursor.shared_second.v1",
                        path_kind="sqlite_db",
                        source_kind="sqlite",
                        files=("state.vscdb",),
                    ),
                ),
            ),
        ),
    )
    monkeypatch.setattr(store_catalog, "CATALOG", fake_catalog)

    backends = agentgrep.BackendSelection(None, None, None)
    sources = agentgrep.discover_from_catalog(tmp_path, "cursor", base, backends)

    matching = [s for s in sources if s.path == target]
    assert len(matching) == 1, [s.adapter_id for s in matching]


def test_descriptor_round_trips_through_json() -> None:
    """Pydantic dump/load identity — guards against future field-name drift."""
    sample = CATALOG.stores[0]
    payload = sample.model_dump_json()
    restored = StoreDescriptor.model_validate_json(payload)
    assert restored == sample


PRIMARY_FIXTURES: tuple[tuple[str, str], ...] = (
    ("claude.projects.session", "example.jsonl"),
    ("claude.projects.subagent", "example.jsonl"),
    ("codex.history", "example.jsonl"),
    ("codex.sessions", "rollout-2026-05-17T12-00-00-example.jsonl"),
    ("gemini.tmp.chats", "session-2026-05-17T12-00-00-example.jsonl"),
    ("gemini.tmp.logs", "logs.json"),
    ("cursor.cli.transcripts", "example.jsonl"),
    ("cursor.cli.plans", "example.plan.md"),
)


def test_primary_fixtures_exist_and_are_well_formed() -> None:
    """Every primary-chat / prompt-history store has at least one fixture sample."""
    from tests.conftest import fixture_path

    for store_id, filename in PRIMARY_FIXTURES:
        path = fixture_path(store_id, filename)
        assert path.is_file()
        content = path.read_text(encoding="utf-8")
        assert content.strip(), f"{store_id} fixture is empty"
        if path.suffix == ".jsonl":
            import json

            for line in content.splitlines():
                if not line.strip():
                    continue
                json.loads(line)  # raises on bad lines
        elif path.suffix == ".json":
            import json

            json.loads(content)


__all__: tuple[str, ...] = ()


# Quiet the unused-import warning for the typing-only import.
_ = t
