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
    StoreCoverage,
    StoreDescriptor,
    StoreFormat,
    StoreRole,
    VersionDetectionStrategy,
)

KNOWN_AGENTS: tuple[AgentName, ...] = (
    "claude",
    "cursor-cli",
    "cursor-ide",
    "codex",
    "gemini",
    "grok",
    "pi",
)
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


def test_catalog_exposes_coverage_levels() -> None:
    """Coverage level separates default search from broader storage support."""
    coverage_by_id = {store.store_id: store.coverage_level for store in CATALOG.stores}

    assert coverage_by_id["claude.history"] is StoreCoverage.DEFAULT_SEARCH
    assert coverage_by_id["claude.store_db"] is StoreCoverage.INSPECTABLE
    assert coverage_by_id["codex.state_db"] is StoreCoverage.INSPECTABLE
    assert coverage_by_id["codex.auth"] is StoreCoverage.PRIVATE


def test_catalog_covers_remaining_claude_and_codex_storage_map() -> None:
    """Claude/Codex catalog rows include non-prompt app/cache/private stores."""
    store_ids = {store.store_id for store in CATALOG.stores}

    assert {
        "claude.credentials",
        "claude.update_state",
        "claude.stats_cache",
        "claude.debug_logs",
        "claude.backups",
        "claude.generic_cache",
        "claude.memory_files",
        "claude.project_instructions",
        "claude.commands",
        "claude.uploads",
        "claude.chrome",
        "claude.native_install",
        "claude.jobs",
        "codex.installation_id",
        "codex.update_check",
        "codex.version_file",
        "codex.personality_migration",
        "codex.config_backups",
        "codex.skills",
        "codex.rules",
        "codex.project_config",
        "codex.project_skills",
        "codex.hooks",
        "codex.plugin_marketplace",
        "codex.secrets",
        "codex.env",
        "codex.arg0_runtime",
        "codex.sqlite_sidecars",
    } <= store_ids

    assert CATALOG.by_id("claude.credentials").coverage_level is StoreCoverage.PRIVATE
    assert CATALOG.by_id("claude.commands").coverage_level is StoreCoverage.INSPECTABLE
    assert CATALOG.by_id("claude.plugins_cache").coverage_level is StoreCoverage.INSPECTABLE
    assert CATALOG.by_id("codex.rules").coverage_level is StoreCoverage.INSPECTABLE
    assert CATALOG.by_id("codex.secrets").coverage_level is StoreCoverage.PRIVATE
    assert CATALOG.by_id("codex.sqlite_sidecars").coverage_level is StoreCoverage.CATALOG_ONLY


def test_catalog_exposes_version_detection_strategies() -> None:
    """Descriptors declare how runtime source versions should be detected."""
    codex_history = CATALOG.by_id("codex.history")
    codex_state = CATALOG.by_id("codex.state_db")
    claude_projects = CATALOG.by_id("claude.projects.session")

    assert VersionDetectionStrategy.SHAPE_INFERENCE in codex_history.version_strategies
    assert VersionDetectionStrategy.VERSION_CHECK in codex_history.version_strategies
    assert VersionDetectionStrategy.SHAPE_INFERENCE in codex_state.version_strategies
    assert VersionDetectionStrategy.EMBEDDED_METADATA in claude_projects.version_strategies

    data_versions = {
        (spec.adapter_id, spec.data_version)
        for store in (codex_history, codex_state, claude_projects)
        for spec in store.discovery
    }
    assert ("codex.history_json.v1", "codex.history_json.legacy") in data_versions
    assert ("codex.history_jsonl.v1", "codex.history_jsonl.current") in data_versions
    assert ("codex.state_sqlite.v1", "codex.state.sqlite.v5") in data_versions
    assert ("claude.projects_jsonl.v1", "claude.projects_jsonl.message.v1") in data_versions


def test_claude_codex_discovered_adapters_are_sampleable_or_explicitly_opaque() -> None:
    """Discovered Claude/Codex adapters must either parse samples or be intentionally opaque."""
    import agentgrep

    dispatchable = set(agentgrep.ITER_SOURCE_RECORD_ADAPTERS)
    missing = [
        f"{store.store_id}:{spec.adapter_id}"
        for store in CATALOG.stores
        if store.agent in {"claude", "codex"}
        and store.coverage_level is not StoreCoverage.PRIVATE
        and store.format is not StoreFormat.OPAQUE
        for spec in store.discovery
        if spec.adapter_id not in dispatchable
    ]

    assert missing == []


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
    assert "cursor_cli.ai_tracking_sqlite.v1" in runtime_adapter_ids
    assert "cursor_ide.state_vscdb_modern.v1" in runtime_adapter_ids
    assert "cursor_ide.state_vscdb_legacy.v1" in runtime_adapter_ids
    assert "cursor_cli.transcripts_jsonl.v1" in runtime_adapter_ids
    assert "gemini.tmp_chats_jsonl.v1" in runtime_adapter_ids
    assert "gemini.tmp_logs_json.v1" in runtime_adapter_ids
    assert "grok.prompt_history_jsonl.v1" in runtime_adapter_ids
    assert "grok.sessions_jsonl.v1" in runtime_adapter_ids
    assert "grok.session_search_sqlite.v1" in runtime_adapter_ids
    assert "pi.sessions_jsonl.v1" in runtime_adapter_ids

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


def test_discover_from_catalog_can_include_non_default_coverage(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-default coverage is inventory-only unless explicitly requested."""
    import agentgrep
    import agentgrep.store_catalog as store_catalog

    base = tmp_path / "base"
    base.mkdir()
    default_file = base / "default.jsonl"
    inspectable_file = base / "inspectable.sqlite"
    catalog_file = base / "catalog.json"
    private_file = base / "private.json"
    for path in (default_file, inspectable_file, catalog_file, private_file):
        _ = path.write_text("{}", encoding="utf-8")

    fake_catalog = StoreCatalog(
        catalog_version=999,
        captured_at=OBSERVED_AT,
        stores=(
            StoreDescriptor(
                agent="codex",
                store_id="codex.test_default",
                role=StoreRole.PROMPT_HISTORY,
                format=StoreFormat.JSONL,
                path_pattern="${HOME}/default.jsonl",
                observed_version="test",
                observed_at=OBSERVED_AT,
                schema_notes="Default-search test row.",
                search_by_default=True,
                discovery=(
                    DiscoverySpec(
                        store="codex.test_default",
                        adapter_id="codex.test_default.v1",
                        path_kind="history_file",
                        source_kind="jsonl",
                        files=("default.jsonl",),
                    ),
                ),
            ),
            StoreDescriptor(
                agent="codex",
                store_id="codex.test_inspectable",
                role=StoreRole.APP_STATE,
                format=StoreFormat.SQLITE,
                path_pattern="${HOME}/inspectable.sqlite",
                coverage=StoreCoverage.INSPECTABLE,
                observed_version="test",
                observed_at=OBSERVED_AT,
                schema_notes="Inspectable test row.",
                search_by_default=False,
                discovery=(
                    DiscoverySpec(
                        store="codex.test_inspectable",
                        adapter_id="codex.test_inspectable.v1",
                        path_kind="sqlite_db",
                        source_kind="sqlite",
                        files=("inspectable.sqlite",),
                    ),
                ),
            ),
            StoreDescriptor(
                agent="codex",
                store_id="codex.test_catalog",
                role=StoreRole.APP_STATE,
                format=StoreFormat.JSON_OBJECT,
                path_pattern="${HOME}/catalog.json",
                coverage=StoreCoverage.CATALOG_ONLY,
                observed_version="test",
                observed_at=OBSERVED_AT,
                schema_notes="Catalog-only test row.",
                search_by_default=False,
                discovery=(
                    DiscoverySpec(
                        store="codex.test_catalog",
                        adapter_id="codex.test_catalog.v1",
                        path_kind="store_file",
                        source_kind="json",
                        files=("catalog.json",),
                    ),
                ),
            ),
            StoreDescriptor(
                agent="codex",
                store_id="codex.test_private",
                role=StoreRole.APP_STATE,
                format=StoreFormat.JSON_OBJECT,
                path_pattern="${HOME}/private.json",
                coverage=StoreCoverage.PRIVATE,
                observed_version="test",
                observed_at=OBSERVED_AT,
                schema_notes="Private test row.",
                search_by_default=False,
                discovery=(
                    DiscoverySpec(
                        store="codex.test_private",
                        adapter_id="codex.test_private.v1",
                        path_kind="store_file",
                        source_kind="json",
                        files=("private.json",),
                    ),
                ),
            ),
        ),
    )
    monkeypatch.setattr(store_catalog, "CATALOG", fake_catalog)

    backends = agentgrep.BackendSelection(None, None, None)
    default_sources = agentgrep.discover_from_catalog(tmp_path, "codex", base, backends)
    all_sources = agentgrep.discover_from_catalog(
        tmp_path,
        "codex",
        base,
        backends,
        include_non_default=True,
    )

    assert {source.path for source in default_sources} == {default_file}
    assert {source.path for source in all_sources} == {
        default_file,
        inspectable_file,
        catalog_file,
    }
    assert {source.coverage for source in all_sources} == {
        StoreCoverage.DEFAULT_SEARCH,
        StoreCoverage.INSPECTABLE,
        StoreCoverage.CATALOG_ONLY,
    }


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
                agent="cursor-cli",
                store_id="cursor-cli.test.shared",
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
    sources = agentgrep.discover_from_catalog(tmp_path, "cursor-cli", base, backends)

    matching = [s for s in sources if s.path == target]
    assert len(matching) == 1, [s.adapter_id for s in matching]


def test_discovery_spec_excludes_required_path_parts(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A glob can include main transcripts while excluding nested side stores."""
    import agentgrep
    import agentgrep.store_catalog as store_catalog

    base = tmp_path / "base"
    main = base / "project" / "session.jsonl"
    excluded = base / "project" / "session" / "subagents" / "agent-a.jsonl"
    main.parent.mkdir(parents=True)
    excluded.parent.mkdir(parents=True)
    _ = main.write_text("{}", encoding="utf-8")
    _ = excluded.write_text("{}", encoding="utf-8")

    fake_catalog = StoreCatalog(
        catalog_version=999,
        captured_at=OBSERVED_AT,
        stores=(
            StoreDescriptor(
                agent="claude",
                store_id="claude.test.main",
                role=StoreRole.PRIMARY_CHAT,
                format=StoreFormat.JSONL,
                path_pattern="${HOME}/test/main",
                observed_version="test",
                observed_at=OBSERVED_AT,
                schema_notes="Test row excluding nested side stores.",
                search_by_default=True,
                discovery=(
                    DiscoverySpec(
                        store="claude.test_main",
                        adapter_id="claude.test_main.v1",
                        path_kind="session_file",
                        source_kind="jsonl",
                        glob="*.jsonl",
                        path_parts_excluded=("subagents",),
                    ),
                ),
            ),
        ),
    )
    monkeypatch.setattr(store_catalog, "CATALOG", fake_catalog)

    backends = agentgrep.BackendSelection(None, None, None)
    sources = agentgrep.discover_from_catalog(tmp_path, "claude", base, backends)

    paths = {source.path for source in sources}
    assert main in paths
    assert excluded not in paths


def test_actual_claude_discovery_splits_main_and_subagent_transcripts(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude main and subagent transcript files get distinct runtime stores."""
    import agentgrep

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    main = home / ".claude" / "projects" / "project" / "session.jsonl"
    subagent = home / ".claude" / "projects" / "project" / "session" / "subagents" / "agent-a.jsonl"
    main.parent.mkdir(parents=True)
    subagent.parent.mkdir(parents=True)
    _ = main.write_text("{}", encoding="utf-8")
    _ = subagent.write_text("{}", encoding="utf-8")

    sources = agentgrep.discover_sources(
        home,
        ("claude",),
        agentgrep.BackendSelection(None, None, None),
    )

    stores_by_path = {source.path: source.store for source in sources}
    assert stores_by_path[main] == "claude.projects"
    assert stores_by_path[subagent] == "claude.projects_subagents"


def test_actual_cursor_discovery_splits_main_and_subagent_transcripts(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor CLI subagent transcript files do not collapse into main sessions."""
    import agentgrep

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    main = home / ".cursor" / "projects" / "project" / "agent-transcripts" / "s" / "s.jsonl"
    subagent = (
        home
        / ".cursor"
        / "projects"
        / "project"
        / "agent-transcripts"
        / "s"
        / "subagents"
        / "agent-a.jsonl"
    )
    main.parent.mkdir(parents=True)
    subagent.parent.mkdir(parents=True)
    _ = main.write_text("{}", encoding="utf-8")
    _ = subagent.write_text("{}", encoding="utf-8")

    sources = agentgrep.discover_sources(
        home,
        ("cursor-cli",),
        agentgrep.BackendSelection(None, None, None),
    )

    stores_by_path = {source.path: source.store for source in sources}
    assert stores_by_path[main] == "cursor-cli.transcripts"
    assert stores_by_path[subagent] == "cursor-cli.subagent_transcripts"


def test_descriptor_round_trips_through_json() -> None:
    """Pydantic dump/load identity — guards against future field-name drift."""
    sample = CATALOG.stores[0]
    payload = sample.model_dump_json()
    restored = StoreDescriptor.model_validate_json(payload)
    assert restored == sample


PRIMARY_FIXTURES: tuple[tuple[str, str], ...] = (
    ("claude.projects.session", "example.jsonl"),
    ("claude.projects.subagent", "example.jsonl"),
    ("claude.history", "history.jsonl"),
    ("codex.history", "example.jsonl"),
    ("codex.sessions", "rollout-2026-05-17T12-00-00-example.jsonl"),
    ("gemini.tmp.chats", "session-2026-05-17T12-00-00-example.jsonl"),
    ("gemini.tmp.logs", "logs.json"),
    ("cursor-cli.transcripts", "example.jsonl"),
    ("cursor-cli.plans", "example.plan.md"),
    ("cursor-cli.prompt_history", "prompt_history.json"),
    ("grok.prompt_history", "prompt_history.jsonl"),
    ("grok.sessions", "chat_history.jsonl"),
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
