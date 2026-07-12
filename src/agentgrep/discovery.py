"""Store discovery: enumerate read-only agent stores into SourceHandles.

Resolves per-agent store roots from the environment and the store catalog,
runs the storage-version detection from ADR 0001, and yields
:class:`~agentgrep.records.SourceHandle` objects the adapters then parse. It
depends on the record types, the store catalog, and the readers; it sits above
the adapters and below the engine.
"""

from __future__ import annotations

import datetime
import logging
import os
import pathlib
import re
import sys
import tomllib
import typing as t
import urllib.parse

from agentgrep.origin import (
    PRUNABLE_ORIGIN_FIELDS,
    OriginEncoding,
    ProjectDirCache,
    decode_project_dir,
    origin_cwd_hash,
)
from agentgrep.readers import (
    as_optional_str,
    file_mtime_ns,
    iter_jsonl,
    list_files_matching,
    read_json_file,
)
from agentgrep.records import (
    CONVERSATION_CONTENT_STORES,
    CONVERSATION_STORE_ROLES,
    AgentName,
    BackendSelection,
    DiscoveryRoot,
    DiscoveryStoreRoles,
    DiscoveryVersionContext,
    DiscoveryVersionDetail,
    JSONValue,
    RecordOrigin,
    SourceHandle,
    SourceOriginSummary,
    SourceVersionDetection,
)
from agentgrep.stores import (
    DiscoverySpec,
    StoreCoverage,
    StoreDescriptor,
    VersionDetectionConfidence,
    VersionDetectionStrategy,
)

logger = logging.getLogger(__name__)


def discover_sources(
    home: pathlib.Path,
    agents: tuple[AgentName, ...],
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover all known parseable sources for the selected agents.

    ``version_detail`` controls how eagerly source handles are enriched:
    ``"none"`` leaves ``version_detection`` empty for fast search paths,
    ``"catalog"`` attaches low-cost catalog observations, and ``"shape"``
    inspects concrete source shape for inventory surfaces. ``store_roles``
    lets latency-sensitive search paths enumerate only the catalogue roles
    that can satisfy a coarse query scope.
    """
    discovered: list[SourceHandle] = []
    for agent in agents:
        if agent == "codex":
            discovered.extend(
                discover_codex_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "claude":
            discovered.extend(
                discover_claude_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "cursor-cli":
            discovered.extend(
                discover_cursor_cli_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "cursor-ide":
            discovered.extend(
                discover_cursor_ide_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "gemini":
            discovered.extend(
                discover_gemini_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "antigravity-cli":
            discovered.extend(
                discover_antigravity_cli_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "antigravity-ide":
            discovered.extend(
                discover_antigravity_ide_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "grok":
            discovered.extend(
                discover_grok_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "pi":
            discovered.extend(
                discover_pi_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "opencode":
            discovered.extend(
                discover_opencode_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "vscode":
            discovered.extend(
                discover_vscode_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
    discovered.sort(key=lambda item: (item.agent, item.store, str(item.path)))
    return discovered


def resolve_env_root(env_var: str, default: pathlib.Path) -> pathlib.Path:
    """Resolve a base directory from an environment variable, with safety.

    When ``env_var`` is set to a non-empty path that is an existing directory,
    return that path. When it is set but points to a non-existent or
    non-directory location, emit a ``WARNING`` log and fall back to
    ``default``. When unset or empty, return ``default``.

    Parameters
    ----------
    env_var : str
        Environment variable name (e.g. ``"CODEX_HOME"``).
    default : pathlib.Path
        Fallback path when the env var is unset, empty, or unusable.

    Returns
    -------
    pathlib.Path
        Resolved base directory.
    """
    value = os.environ.get(env_var)
    if not value:
        return default
    candidate = pathlib.Path(value)
    if candidate.is_dir():
        return candidate
    status = "not_a_directory" if candidate.exists() else "not_found"
    logger.warning(
        "env-override path unavailable, fell back to default",
        extra={
            "agentgrep_env_var": env_var,
            "agentgrep_env_path": value,
            "agentgrep_env_path_status": status,
        },
    )
    return default


def _resolve_optional_root(value: str | None, default: pathlib.Path, *, label: str) -> pathlib.Path:
    """Resolve an optional path override, warning and falling back on bad paths."""
    if not value:
        return default
    candidate = pathlib.Path(os.path.expandvars(value)).expanduser()
    if candidate.is_dir():
        return candidate
    status = "not_a_directory" if candidate.exists() else "not_found"
    logger.warning(
        "path override unavailable, fell back to default",
        extra={
            "agentgrep_override_label": label,
            "agentgrep_override_path": value,
            "agentgrep_override_path_status": status,
        },
    )
    return default


def _codex_sqlite_home_from_config(codex_root: pathlib.Path) -> str | None:
    """Return Codex's configured ``sqlite_home`` value when present."""
    config_path = codex_root / "config.toml"
    if not config_path.is_file():
        return None
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning(
            "codex config parse failed",
            extra={
                "agentgrep_path": str(config_path),
                "agentgrep_error": type(exc).__name__,
            },
        )
        return None
    value = payload.get("sqlite_home")
    return value if isinstance(value, str) else None


def resolve_codex_sqlite_root(codex_root: pathlib.Path) -> pathlib.Path:
    """Resolve Codex's SQLite root from env/config, falling back to ``CODEX_HOME``."""
    env_value = os.environ.get("CODEX_SQLITE_HOME")
    if env_value:
        return _resolve_optional_root(env_value, codex_root, label="CODEX_SQLITE_HOME")
    return _resolve_optional_root(
        _codex_sqlite_home_from_config(codex_root),
        codex_root,
        label="sqlite_home",
    )


def _first_jsonl_mapping(path: pathlib.Path) -> dict[str, JSONValue] | None:
    """Return the first object record from a JSONL file."""
    for value in iter_jsonl(path):
        if isinstance(value, dict):
            return value
    return None


def _first_json_array_mapping(path: pathlib.Path) -> dict[str, JSONValue] | None:
    """Return the first object from a JSON array file."""
    value = read_json_file(path)
    if not isinstance(value, list):
        return None
    for entry in value:
        if isinstance(entry, dict):
            return entry
    return None


def _json_mapping(path: pathlib.Path) -> dict[str, JSONValue] | None:
    """Return a JSON file payload when its top-level value is an object."""
    value = read_json_file(path)
    return value if isinstance(value, dict) else None


def _safe_project_root(value: object) -> pathlib.Path | None:
    """Return a usable project root from session metadata."""
    if not isinstance(value, str) or not value:
        return None
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir():
        return None
    return path


def _project_roots_from_jsonl_sessions(
    session_root: pathlib.Path,
    backends: BackendSelection,
) -> tuple[pathlib.Path, ...]:
    """Derive known project roots from session metadata JSONL files."""
    if not session_root.exists():
        return ()
    roots: set[pathlib.Path] = set()
    for path in list_files_matching(session_root, "*.jsonl", backends.find_tool):
        if "subagents" in path.parts:
            continue
        for index, record in enumerate(iter_jsonl(path)):
            if not isinstance(record, dict):
                if index >= 31:
                    break
                continue
            mapping = t.cast("dict[str, object]", record)
            payload = mapping.get("payload")
            candidates = [mapping.get("cwd"), mapping.get("project")]
            if isinstance(payload, dict):
                payload_mapping = t.cast("dict[str, object]", payload)
                candidates.extend((payload_mapping.get("cwd"), payload_mapping.get("project")))
            found_root = False
            for candidate in candidates:
                root = _safe_project_root(candidate)
                if root is not None:
                    roots.add(root)
                    found_root = True
                    break
            if found_root or index >= 31:
                break
    return tuple(sorted(roots))


def _codex_project_roots_from_legacy_sessions(
    session_root: pathlib.Path,
    backends: BackendSelection,
) -> tuple[pathlib.Path, ...]:
    """Derive known project roots from legacy Codex JSON session files."""
    if not session_root.exists():
        return ()
    roots: set[pathlib.Path] = set()
    for path in list_files_matching(session_root, "rollout-*.json", backends.find_tool):
        payload = read_json_file(path)
        if not isinstance(payload, dict):
            continue
        session = payload.get("session")
        if not isinstance(session, dict):
            continue
        mapping = t.cast("dict[str, object]", session)
        root = _safe_project_root(mapping.get("cwd") or mapping.get("project"))
        if root is not None:
            roots.add(root)
    return tuple(sorted(roots))


def _claude_project_roots(
    root: pathlib.Path,
    backends: BackendSelection,
) -> tuple[pathlib.Path, ...]:
    """Return project roots Claude Code has already referenced in transcripts."""
    return _project_roots_from_jsonl_sessions(root / "projects", backends)


def _codex_project_roots(
    root: pathlib.Path,
    backends: BackendSelection,
) -> tuple[pathlib.Path, ...]:
    """Return project roots Codex has already referenced in transcripts."""
    session_root = root / "sessions"
    return tuple(
        sorted(
            {
                *_project_roots_from_jsonl_sessions(session_root, backends),
                *_codex_project_roots_from_legacy_sessions(session_root, backends),
            },
        ),
    )


def _codex_client_version_from_cache(codex_root: pathlib.Path | None) -> str | None:
    """Return Codex's local client-version hint without spawning the CLI."""
    if codex_root is None:
        return None
    value = read_json_file(codex_root / "models_cache.json")
    if not isinstance(value, dict):
        return None
    return as_optional_str(value.get("client_version"))


def _catalog_version_detection(
    descriptor: StoreDescriptor,
    spec: DiscoverySpec,
    *,
    app_version: str | None = None,
) -> SourceVersionDetection:
    """Build the low-confidence fallback for sources without shape evidence."""
    return SourceVersionDetection(
        app_version=app_version,
        data_version=spec.data_version,
        strategy=VersionDetectionStrategy.CATALOG_OBSERVATION,
        confidence=VersionDetectionConfidence.LOW,
        evidence=f"catalog observed_version: {descriptor.observed_version}",
    )


def _codex_source_version_detection(
    source: SourceHandle,
    descriptor: StoreDescriptor,
    spec: DiscoverySpec,
    context: DiscoveryVersionContext,
) -> SourceVersionDetection:
    """Detect Codex source versions from local metadata and concrete shape."""
    app_version = context.codex_client_version

    if source.adapter_id == "codex.history_jsonl.v1":
        record = _first_jsonl_mapping(source.path)
        if record is not None and {"session_id", "ts", "text"}.issubset(record):
            return SourceVersionDetection(
                app_version=app_version,
                data_version="codex.history_jsonl.current",
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="history.jsonl object keys include session_id, ts, text",
            )
    elif source.adapter_id == "codex.history_json.v1":
        record = _first_json_array_mapping(source.path)
        if record is not None and {"command", "timestamp"}.issubset(record):
            return SourceVersionDetection(
                app_version=app_version,
                data_version="codex.history_json.legacy",
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="history.json array object keys include command, timestamp",
            )
    elif source.adapter_id == "codex.sessions_jsonl.v1":
        record = _first_jsonl_mapping(source.path)
        if record is not None and record.get("type") == "session_meta":
            payload = record.get("payload")
            embedded_version: str | None = None
            if isinstance(payload, dict):
                embedded_version = as_optional_str(payload.get("cli_version"))
            if embedded_version:
                return SourceVersionDetection(
                    app_version=embedded_version,
                    data_version=spec.data_version,
                    strategy=VersionDetectionStrategy.EMBEDDED_METADATA,
                    confidence=VersionDetectionConfidence.HIGH,
                    evidence="session_meta.payload keys include cli_version",
                )
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="jsonl event type includes session_meta",
            )
    elif source.adapter_id == "codex.sessions_legacy_json.v1":
        record = _json_mapping(source.path)
        if record is not None and {"session", "items"}.issubset(record):
            return SourceVersionDetection(
                app_version=app_version,
                data_version="codex.sessions.legacy_json.v1",
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="legacy session JSON object keys include session, items",
            )
    elif source.adapter_id == "codex.session_index_jsonl.v1":
        record = _first_jsonl_mapping(source.path)
        if record is not None and {"id", "thread_name", "updated_at"}.issubset(record):
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="session_index.jsonl object keys include id, thread_name, updated_at",
            )
    elif source.adapter_id == "codex.external_imports_json.v1":
        record = _json_mapping(source.path)
        if record is not None and "records" in record:
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="external import ledger object key includes records",
            )
    elif source.adapter_id == "codex.memories_text.v1":
        return SourceVersionDetection(
            app_version=app_version,
            data_version=spec.data_version,
            strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
            confidence=VersionDetectionConfidence.MEDIUM,
            evidence="markdown memory file discovered under memories",
        )
    elif source.adapter_id in {
        "codex.config_toml.v1",
        "codex.config_backup_toml.v1",
        "codex.project_config_toml.v1",
    }:
        try:
            payload = tomllib.loads(source.path.read_text(encoding="utf-8"))
        except OSError, tomllib.TOMLDecodeError:
            payload = {}
        if payload:
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="TOML top-level keys observed",
            )
    elif source.adapter_id == "codex.app_state_json_summary.v1":
        record = _json_mapping(source.path)
        if record is not None:
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="app-state JSON object keys observed",
            )
    elif source.adapter_id == "codex.plugin_manifest_json.v1":
        record = _json_mapping(source.path)
        if record is not None and {"name", "description"}.intersection(record):
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="plugin manifest JSON object keys observed",
            )
    elif source.adapter_id in {
        "codex.hooks_json.v1",
        "codex.plugin_hooks_json.v1",
        "codex.plugin_marketplace_json.v1",
    }:
        record = _json_mapping(source.path)
        if record is not None:
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="JSON object keys observed for Codex hook or plugin metadata",
            )
    elif source.adapter_id in {
        "codex.plugin_instruction_text.v1",
        "codex.project_skill_text.v1",
        "codex.rules_text.v1",
        "codex.skills_text.v1",
    }:
        return SourceVersionDetection(
            app_version=app_version,
            data_version=spec.data_version,
            strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
            confidence=VersionDetectionConfidence.MEDIUM,
            evidence="instruction text file discovered for Codex",
        )
    elif source.adapter_id == "codex.file_metadata_summary.v1":
        return SourceVersionDetection(
            app_version=app_version,
            data_version=spec.data_version,
            strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
            confidence=VersionDetectionConfidence.LOW,
            evidence="metadata-only raw state file observed",
        )
    elif source.source_kind == "sqlite" and spec.data_version is not None:
        match = re.fullmatch(r".+_([0-9]+)\.sqlite", source.path.name)
        if match is not None:
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence=f"filename suffix _{match.group(1)}.sqlite",
            )

    return _catalog_version_detection(descriptor, spec, app_version=app_version)


def _claude_source_version_detection(
    source: SourceHandle,
    descriptor: StoreDescriptor,
    spec: DiscoverySpec,
) -> SourceVersionDetection:
    """Detect Claude Code source versions from embedded metadata and shape."""
    if source.adapter_id == "claude.history_jsonl.v1":
        record = _first_jsonl_mapping(source.path)
        if record is not None and {"display", "timestamp", "project"}.issubset(record):
            return SourceVersionDetection(
                app_version=None,
                data_version="claude.history_jsonl.log_entry.v1",
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="history.jsonl object keys include display, timestamp, project",
            )
    elif source.adapter_id == "claude.projects_jsonl.v1":
        record = _first_jsonl_mapping(source.path)
        if record is not None:
            app_version = as_optional_str(record.get("version")) or as_optional_str(
                record.get("claude_code_version"),
            )
            if app_version:
                return SourceVersionDetection(
                    app_version=app_version,
                    data_version=spec.data_version,
                    strategy=VersionDetectionStrategy.EMBEDDED_METADATA,
                    confidence=VersionDetectionConfidence.HIGH,
                    evidence="project transcript keys include version",
                )
            if {"type", "sessionId", "message"}.issubset(record):
                return SourceVersionDetection(
                    app_version=None,
                    data_version=spec.data_version,
                    strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                    confidence=VersionDetectionConfidence.MEDIUM,
                    evidence="project transcript keys include type, sessionId, message",
                )
    elif source.adapter_id == "claude.tasks_json.v1":
        record = _json_mapping(source.path)
        if record is not None and {"id", "subject", "description", "status"}.issubset(record):
            return SourceVersionDetection(
                app_version=None,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="task JSON object keys include id, subject, description, status",
            )
    elif source.adapter_id == "claude.settings_json.v1":
        record = _json_mapping(source.path)
        if record is not None:
            return SourceVersionDetection(
                app_version=None,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="settings JSON object keys observed",
            )
    elif source.adapter_id == "claude.todos_json.v1":
        record = _json_mapping(source.path)
        if record is not None:
            return SourceVersionDetection(
                app_version=None,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="todo JSON object keys observed",
            )
    elif source.adapter_id == "claude.teams_json.v1":
        record = _json_mapping(source.path)
        if record is not None and {"name", "members"}.issubset(record):
            return SourceVersionDetection(
                app_version=None,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="team config JSON object keys include name, members",
            )
    elif source.adapter_id == "claude.app_state_json_summary.v1":
        record = _json_mapping(source.path)
        if record is not None:
            return SourceVersionDetection(
                app_version=None,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="app-state JSON object keys observed",
            )
    elif source.adapter_id in {
        "claude.plugin_hooks_json.v1",
        "claude.plugin_manifest_json.v1",
    }:
        record = _json_mapping(source.path)
        if record is not None:
            return SourceVersionDetection(
                app_version=None,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="plugin JSON object keys observed",
            )
    elif source.adapter_id in {
        "claude.commands_text.v1",
        "claude.memory_text.v1",
        "claude.plugin_instruction_text.v1",
        "claude.project_instruction_text.v1",
        "claude.projects_memory_text.v1",
        "claude.session_memory_text.v1",
        "claude.skills_text.v1",
    }:
        return SourceVersionDetection(
            app_version=None,
            data_version=spec.data_version,
            strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
            confidence=VersionDetectionConfidence.MEDIUM,
            evidence="instruction or memory text file discovered for Claude",
        )
    elif source.adapter_id == "claude.file_metadata_summary.v1":
        return SourceVersionDetection(
            app_version=None,
            data_version=spec.data_version,
            strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
            confidence=VersionDetectionConfidence.LOW,
            evidence="metadata-only raw state file observed",
        )

    return _catalog_version_detection(descriptor, spec)


def detect_source_version(
    source: SourceHandle,
    descriptor: StoreDescriptor,
    spec: DiscoverySpec,
    context: DiscoveryVersionContext,
) -> SourceVersionDetection:
    """Detect concrete source version metadata for discovery payloads."""
    if source.agent == "codex":
        return _codex_source_version_detection(source, descriptor, spec, context)
    if source.agent == "claude":
        return _claude_source_version_detection(source, descriptor, spec)
    return _catalog_version_detection(descriptor, spec)


def build_discovery_version_context(
    agent: AgentName,
    primary_roots: dict[str, pathlib.Path],
    version_detail: DiscoveryVersionDetail,
) -> DiscoveryVersionContext:
    """Build cached version metadata for a single discovery pass."""
    codex_client_version: str | None = None
    if agent == "codex" and version_detail != "none":
        codex_client_version = _codex_client_version_from_cache(primary_roots.get("default"))
    return DiscoveryVersionContext(codex_client_version=codex_client_version)


def handles_from_discovery(
    spec: DiscoverySpec,
    agent: AgentName,
    root: pathlib.Path,
    backends: BackendSelection,
    coverage: StoreCoverage,
    *,
    project_dirs: ProjectDirCache | None = None,
) -> list[SourceHandle]:
    """Produce ``SourceHandle``s from a :class:`DiscoverySpec`.

    Applies the spec's ``home_subpath`` under ``root`` to derive the search
    root, then enumerates source files via ``files`` (single-file lookups),
    ``glob`` (recursive walk with optional path-part filters), and
    ``platform_paths`` (absolute paths).

    ``project_dirs`` is the caller's memo for the filesystem-probing decode of
    a dash-encoded project directory name (see :func:`_source_origin_summary`).
    One project directory backs every transcript of every session it ran, so
    the decode is worth remembering — but only for as long as the discovery
    pass that owns it, since the directory layout it probed can change.
    """
    sources: list[SourceHandle] = []
    search_root = root.joinpath(*spec.home_subpath) if spec.home_subpath else root

    for name in spec.files:
        candidate = search_root / name
        if candidate.is_file():
            sources.append(
                SourceHandle(
                    agent=agent,
                    store=spec.store,
                    adapter_id=spec.adapter_id,
                    path=candidate,
                    path_kind=spec.path_kind,
                    source_kind=spec.source_kind,
                    search_root=None,
                    mtime_ns=file_mtime_ns(candidate),
                    coverage=coverage,
                    origin_summary=_source_origin_summary(
                        spec,
                        candidate,
                        project_dirs=project_dirs,
                    ),
                ),
            )

    if spec.glob is not None and search_root.exists():
        required_parts = set(spec.path_parts_required)
        excluded_parts = set(spec.path_parts_excluded)
        for path in list_files_matching(search_root, spec.glob, backends.find_tool):
            if required_parts and not required_parts.issubset(path.parts):
                continue
            if excluded_parts and excluded_parts.intersection(path.parts):
                continue
            sources.append(
                SourceHandle(
                    agent=agent,
                    store=spec.store,
                    adapter_id=spec.adapter_id,
                    path=path,
                    path_kind=spec.path_kind,
                    source_kind=spec.source_kind,
                    search_root=search_root,
                    mtime_ns=file_mtime_ns(path),
                    coverage=coverage,
                    origin_summary=_source_origin_summary(spec, path, project_dirs=project_dirs),
                ),
            )

    for absolute_path_str in spec.platform_paths:
        candidate = pathlib.Path(absolute_path_str).expanduser()
        if candidate.is_file():
            sources.append(
                SourceHandle(
                    agent=agent,
                    store=spec.store,
                    adapter_id=spec.adapter_id,
                    path=candidate,
                    path_kind=spec.path_kind,
                    source_kind=spec.source_kind,
                    search_root=None,
                    mtime_ns=file_mtime_ns(candidate),
                    coverage=coverage,
                    origin_summary=_source_origin_summary(
                        spec,
                        candidate,
                        project_dirs=project_dirs,
                    ),
                ),
            )

    return sources


_CURSOR_CLI_TRANSCRIPT_STORES: frozenset[str] = frozenset(
    {"cursor-cli.transcripts", "cursor-cli.subagent_transcripts"},
)
"""Cursor CLI stores whose working directory exists only in the path."""

_CURSOR_CLI_PROJECT_PARENT = "agent-transcripts"
"""Path segment the dash-encoded Cursor CLI project directory sits above."""


def _source_origin_summary(
    spec: DiscoverySpec,
    path: pathlib.Path,
    *,
    project_dirs: ProjectDirCache | None = None,
) -> SourceOriginSummary | None:
    """Return source-level origin facts known from discovery metadata.

    ``complete_fields`` is the pruning claim, so it is filtered through
    :data:`~agentgrep.origin.PRUNABLE_ORIGIN_FIELDS`. The workspace ``cwd``
    read from ``workspace.json`` stays on the summary as a *fact* — it is what
    the workspace points at — but it is never claimed complete: a
    ``composerData`` bubble carries its own ``cwd``, and claiming completeness
    for a value the parser can contradict prunes the very record the user
    asked for. The Cursor CLI project directory is the same kind of fact and
    gets the same treatment.
    """
    if spec.store in _CURSOR_CLI_TRANSCRIPT_STORES:
        cwd = _cursor_cli_project_cwd(path, cache=project_dirs)
        return None if cwd is None else SourceOriginSummary(origins=(RecordOrigin(cwd=cwd),))
    if spec.store != "cursor-ide.workspace_state" or path.name != "state.vscdb":
        return None
    cwd_hash = origin_cwd_hash(path.parent.name)
    if cwd_hash is None:
        return None
    return SourceOriginSummary(
        origins=(RecordOrigin(cwd=_cursor_workspace_state_cwd(path), cwd_hash=cwd_hash),),
        complete_fields=PRUNABLE_ORIGIN_FIELDS,
    )


def _cursor_cli_project_cwd(
    path: pathlib.Path,
    *,
    cache: ProjectDirCache | None,
) -> str | None:
    """Recover the working directory Cursor CLI dash-encoded into ``path``.

    A transcript lives at
    ``~/.cursor/projects/<name>/agent-transcripts/<session>/…``, and ``<name>``
    is the absolute working directory with every separator replaced by ``-``.
    Nothing inside the JSONL says where the session ran, so this is the only
    place to learn it — and the encoding is lossy, so
    :func:`~agentgrep.origin.decode_project_dir` answers only when exactly one
    reconstruction exists on disk.

    Resolving it here rather than in the adapter means the filesystem-probing
    decode runs once per project *name* per discovery pass instead of once per
    transcript file, and the memo dies with the pass that owns it.
    """
    parts = path.parts
    for index in range(len(parts) - 1, 0, -1):
        if parts[index] != _CURSOR_CLI_PROJECT_PARENT:
            continue
        return decode_project_dir(
            parts[index - 1],
            encoding=OriginEncoding.DASH,
            cache=cache,
        )
    return None


def _cursor_workspace_state_cwd(path: pathlib.Path) -> str | None:
    payload = read_json_file(path.parent / "workspace.json")
    if not isinstance(payload, dict):
        return None
    folder = as_optional_str(t.cast("dict[str, object]", payload).get("folder"))
    if not folder:
        return None
    return _workspace_uri_to_path(folder)


def _workspace_uri_to_path(uri: str) -> str | None:
    remote = re.match(r"vscode-remote://[^/]+(/.*)$", uri)
    if remote:
        return urllib.parse.unquote(remote.group(1)) or None
    if uri.startswith("file://"):
        return urllib.parse.unquote(uri[len("file://") :]) or None
    return None


def format_timestamp_tig(value: str | None) -> str:
    """Render an ISO-8601 timestamp as ``YYYY-MM-DD HH:MM ±HHMM`` (tig style).

    Localizes to the system timezone before formatting so the displayed
    time matches what the user expects to see — tig's main view does the
    same. Returns ``""`` for ``None`` / empty input and a clipped raw
    string for unparseable input so callers can pad consistently.

    Examples
    --------
    >>> format_timestamp_tig(None)
    ''
    >>> format_timestamp_tig("")
    ''
    >>> # An ISO timestamp with explicit timezone — formatted result keeps
    >>> # the offset for the system's local timezone (whose exact value
    >>> # varies by host, so we just check shape here).
    >>> sample = format_timestamp_tig("2026-05-17T11:59:12+00:00")
    >>> len(sample)
    22
    >>> sample[4], sample[7], sample[10], sample[13], sample[16]
    ('-', '-', ' ', ':', ' ')
    >>> format_timestamp_tig("not-a-real-timestamp")
    'not-a-real-timestamp'
    """
    if not value:
        return ""
    candidate = value.replace("Z", "+00:00")
    try:
        moment = datetime.datetime.fromisoformat(candidate)
    except ValueError:
        return value[:22]
    return moment.astimezone().strftime("%Y-%m-%d %H:%M %z")


def descriptor_admits_store_roles(
    descriptor: StoreDescriptor,
    store_roles: DiscoveryStoreRoles,
) -> bool:
    """Return whether a catalogue row can serve a role-narrowed discovery pass.

    A row normally qualifies on its own ``role``. The exception is the
    conversation surface: the app-state rows in
    :data:`agentgrep.records.CONVERSATION_CONTENT_STORES` hold conversation
    content, and a role check alone would leave them unreachable at every scope.
    Admitting them here — coarsely, per descriptor, before any filesystem walk —
    keeps the walk narrow; ``source_matches_scope`` narrows precisely afterwards.

    Parameters
    ----------
    descriptor : StoreDescriptor
        The catalogue row being considered.
    store_roles : DiscoveryStoreRoles
        Roles the caller's scope can consume, or ``None`` for every role.

    Returns
    -------
    bool
        Whether the row survives role narrowing.
    """
    if store_roles is None:
        return True
    if descriptor.role in store_roles:
        return True
    if not store_roles & CONVERSATION_STORE_ROLES:
        return False
    return any(spec.store in CONVERSATION_CONTENT_STORES for spec in descriptor.discovery)


def discover_from_catalog(
    home: pathlib.Path,
    agent: AgentName,
    base: pathlib.Path | dict[str, DiscoveryRoot],
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Walk every catalogue row for ``agent`` and emit ``SourceHandle``s.

    Each row's :class:`agentgrep.stores.DiscoverySpec` entries drive
    enumeration via :func:`handles_from_discovery`. Named roots may point to
    one directory or a bounded tuple of known project directories. Rows whose
    ``discovery`` tuple is empty are documentary-only and contribute no sources.
    ``DEFAULT_SEARCH`` rows are emitted by default. Inventory callers can
    set ``include_non_default`` to include ``INSPECTABLE`` and
    ``CATALOG_ONLY`` rows that carry discovery specs. ``PRIVATE`` rows are
    never enumerated from disk. ``version_detail`` lets latency-sensitive
    callers skip source-version enrichment until a metadata-rich surface asks
    for it. ``store_roles`` restricts enumeration before any filesystem walk,
    which lets search avoid stores its scope cannot consume — see
    :func:`descriptor_admits_store_roles` for how the conversation surface still
    reaches its allowlisted app-state rows.
    """
    from agentgrep.store_catalog import CATALOG

    roots: dict[str, DiscoveryRoot] = {"default": base} if isinstance(base, pathlib.Path) else base
    primary_roots: dict[str, pathlib.Path] = {}
    for key, value in roots.items():
        if isinstance(value, pathlib.Path):
            primary_roots[key] = value
        elif value:
            primary_roots[key] = value[0]
    version_context = build_discovery_version_context(agent, primary_roots, version_detail)
    # Owned by this pass and discarded with it: the dash decode probes the
    # filesystem, and a memo that outlived the walk would answer from a
    # directory layout that has since changed.
    project_dirs: ProjectDirCache = {}
    sources: list[SourceHandle] = []
    for descriptor in CATALOG.for_agent(agent):
        coverage = descriptor.coverage_level
        if coverage is StoreCoverage.PRIVATE:
            continue
        if not descriptor_admits_store_roles(descriptor, store_roles):
            continue
        if coverage is not StoreCoverage.DEFAULT_SEARCH and not include_non_default:
            continue
        # Per-descriptor dedup: a row whose discovery tuple has more than one
        # spec (e.g. Cursor IDE state.vscdb with both the modern ide_global
        # root and a legacy ~/.cursor glob) must not yield the same file twice
        # under different adapter ids on layouts where both specs match.
        seen_paths: set[pathlib.Path] = set()
        for spec in descriptor.discovery:
            root_value = roots.get(spec.root_key)
            if root_value is None:
                continue
            root_paths = root_value if isinstance(root_value, tuple) else (root_value,)
            for root in root_paths:
                for handle in handles_from_discovery(
                    spec,
                    agent,
                    root,
                    backends,
                    coverage,
                    project_dirs=project_dirs,
                ):
                    if handle.path in seen_paths:
                        continue
                    seen_paths.add(handle.path)
                    if version_detail == "catalog":
                        handle.version_detection = _catalog_version_detection(
                            descriptor,
                            spec,
                            app_version=version_context.codex_client_version
                            if agent == "codex"
                            else None,
                        )
                    elif version_detail == "shape":
                        handle.version_detection = detect_source_version(
                            handle,
                            descriptor,
                            spec,
                            version_context,
                        )
                    sources.append(handle)
    return sources


def discover_codex_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Codex sessions and command history.

    Honours the ``CODEX_HOME`` environment variable (see upstream
    ``codex-rs/utils/home-dir/src/lib.rs``); falls back to ``${HOME}/.codex``
    when unset or empty. Path roots, globs, file lists, and adapter metadata
    come from the ``codex.*`` rows of
    :data:`agentgrep.store_catalog.CATALOG`.
    """
    root = resolve_env_root("CODEX_HOME", home / ".codex")
    if not root.exists():
        return []
    sqlite_root = resolve_codex_sqlite_root(root)
    roots: dict[str, DiscoveryRoot] = {"default": root, "codex_sqlite": sqlite_root}
    if include_non_default:
        roots["codex_project"] = _codex_project_roots(root, backends)
    return discover_from_catalog(
        home,
        "codex",
        roots,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_claude_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Claude Code project session files.

    Honours ``CLAUDE_CONFIG_DIR`` and otherwise falls back to
    ``${HOME}/.claude``. Path roots, globs, and adapter metadata come from
    the ``claude.*`` rows of :data:`agentgrep.store_catalog.CATALOG`.
    """
    root = resolve_env_root("CLAUDE_CONFIG_DIR", home / ".claude")
    if not root.exists():
        return []
    roots: dict[str, DiscoveryRoot] = {"default": root}
    if include_non_default:
        roots["claude_project"] = _claude_project_roots(root, backends)
    return discover_from_catalog(
        home,
        "claude",
        roots,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_cursor_cli_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Cursor CLI (``cursor-agent``) sources.

    Covers the terminal agent's transcripts under ``~/.cursor/projects``,
    the AI-tracking SQLite, and the lowercase ``~/.config/cursor`` home
    (prompt history and chat ``store.db`` blobs). Driven entirely by the
    ``cursor-cli.*`` catalogue rows.
    """
    return discover_from_catalog(
        home,
        "cursor-cli",
        home,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def _cursor_ide_native_user_dir(home: pathlib.Path) -> pathlib.Path:
    """Resolve the native Cursor IDE ``User/`` directory for this platform."""
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Cursor" / "User"
    if sys.platform == "win32":
        return home / "AppData" / "Roaming" / "Cursor" / "User"
    return home / ".config" / "Cursor" / "User"


def _cursor_ide_workspace_root(home: pathlib.Path) -> pathlib.Path:
    """Resolve the Cursor IDE ``workspaceStorage`` directory for this platform."""
    return _cursor_ide_native_user_dir(home) / "workspaceStorage"


def _cursor_ide_user_dirs(home: pathlib.Path) -> tuple[pathlib.Path, ...]:
    """Return existing Cursor IDE ``User/`` directories, including the WSL host mount.

    Mirrors :func:`_vscode_user_dirs`: the native per-platform ``User/`` dir plus,
    on WSL, the Windows-host ``User/`` dirs under the users mount. Cursor is a
    VS Code fork, so a WSL-remote project persists its chat client-side on the
    Windows host (reachable from the distro via ``/mnt/c``).
    ``AGENTGREP_WSL_USERS_ROOT`` overrides the mount root (default
    ``/mnt/c/Users``). See :ref:`adr-cross-host-discovery`.
    """
    bases: list[pathlib.Path] = [_cursor_ide_native_user_dir(home)]
    if _is_wsl():
        windows_users = pathlib.Path(os.environ.get("AGENTGREP_WSL_USERS_ROOT") or "/mnt/c/Users")
        if windows_users.is_dir():
            bases.extend(
                user_dir / "AppData" / "Roaming" / "Cursor" / "User"
                for user_dir in sorted(windows_users.glob("*"))
            )
    return tuple(path for path in dict.fromkeys(bases) if path.is_dir())


def discover_cursor_ide_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Cursor IDE (desktop app) sources.

    Covers the VS Code-style ``state.vscdb`` databases: the global
    ``globalStorage`` location, the legacy ``~/.cursor/state.vscdb`` glob, and
    the per-workspace ``workspaceStorage/<hash>/state.vscdb`` databases. The
    ``ide_global`` and ``ide_workspace`` roots span every existing native and
    (on WSL) Windows-host ``User/`` dir, so a WSL-remote project's IDE chat —
    written client-side on Windows — is reachable. Driven entirely by the
    ``cursor-ide.*`` catalogue rows.
    """
    user_dirs = _cursor_ide_user_dirs(home)
    roots: dict[str, DiscoveryRoot] = {
        "default": home,
        "ide_global": tuple(d / "globalStorage" for d in user_dirs),
        "ide_workspace": tuple(d / "workspaceStorage" for d in user_dirs),
    }
    return discover_from_catalog(
        home,
        "cursor-ide",
        roots,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_gemini_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Gemini CLI sessions and prompt logs.

    Honours the ``GEMINI_CLI_HOME`` environment variable (see upstream
    ``packages/cli/index.ts``); falls back to ``${HOME}/.gemini`` when
    unset or empty. Path roots, globs, and adapter metadata come from the
    ``gemini.*`` rows of :data:`agentgrep.store_catalog.CATALOG`.
    """
    base = resolve_env_root("GEMINI_CLI_HOME", home / ".gemini")
    if not base.exists():
        return []
    return discover_from_catalog(
        home,
        "gemini",
        base,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_antigravity_cli_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Google Antigravity CLI stores under ``~/.gemini``."""
    base = home / ".gemini" / "antigravity-cli"
    if not base.exists():
        return []
    return discover_from_catalog(
        home,
        "antigravity-cli",
        base,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_antigravity_ide_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Google Antigravity IDE stores under ``~/.gemini``."""
    base = home / ".gemini" / "antigravity"
    if not base.exists():
        return []
    return discover_from_catalog(
        home,
        "antigravity-ide",
        base,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_grok_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Grok CLI sessions and prompt history.

    Honours the ``GROK_HOME`` environment variable; falls back to
    ``${HOME}/.grok`` when unset or empty. Path roots, globs, file
    lists, and adapter metadata come from the ``grok.*`` rows of
    :data:`agentgrep.store_catalog.CATALOG`.
    """
    base = resolve_env_root("GROK_HOME", home / ".grok")
    if not base.exists():
        return []
    return discover_from_catalog(
        home,
        "grok",
        base,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_pi_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover pi (earendil-works/pi) session transcripts.

    Honours ``PI_CODING_AGENT_DIR`` (pi's agent data directory, used
    verbatim) and falls back to ``${HOME}/.pi/agent``. The optional
    ``PI_CODING_AGENT_SESSION_DIR`` overrides the sessions directory
    directly: when set, pi writes session files flat into it with no
    per-working-directory subdirectory, so it is resolved as a separate
    discovery root. Path roots, globs, and adapter metadata come from
    the ``pi.*`` rows of :data:`agentgrep.store_catalog.CATALOG`.
    """
    agent_dir = resolve_env_root("PI_CODING_AGENT_DIR", home / ".pi" / "agent")
    session_dir = _resolve_optional_root(
        os.environ.get("PI_CODING_AGENT_SESSION_DIR"),
        agent_dir / "sessions",
        label="PI_CODING_AGENT_SESSION_DIR",
    )
    context_mode_dir = home / ".pi" / "context-mode"
    if not agent_dir.exists() and not session_dir.exists() and not context_mode_dir.exists():
        return []
    roots: dict[str, DiscoveryRoot] = {
        "default": agent_dir,
        "pi_session": session_dir,
        "pi_context_mode": context_mode_dir,
    }
    return discover_from_catalog(
        home,
        "pi",
        roots,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_opencode_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover OpenCode (anomalyco/opencode) SQLite databases.

    OpenCode stores conversations in ``opencode.db`` under its XDG data
    directory (``${XDG_DATA_HOME}/opencode``, falling back to
    ``${HOME}/.local/share/opencode``). The store is discovered by
    filename (not a glob) so the binary SQLite file bypasses the
    text prefilter, the same way the Grok SQLite store is.

    ``OPENCODE_DB`` overrides the database location: when it points at an
    absolute file, OpenCode uses that file (any filename) instead of the
    default, so agentgrep discovers that exact file directly — which also
    makes non-stable channel databases (``opencode-<channel>.db``)
    reachable by pointing ``OPENCODE_DB`` at them. The default lookup and
    adapter metadata come from the ``opencode.*`` rows of
    :data:`agentgrep.store_catalog.CATALOG`.
    """
    db_override = os.environ.get("OPENCODE_DB")
    if db_override and db_override != ":memory:":
        candidate = pathlib.Path(os.path.expandvars(db_override)).expanduser()
        if candidate.is_absolute():
            if not candidate.is_file():
                return []
            from agentgrep.store_catalog import CATALOG

            descriptor = CATALOG.by_id("opencode.db")
            if store_roles is not None and descriptor.role not in store_roles:
                return []
            handle = SourceHandle(
                agent="opencode",
                store="opencode.db",
                adapter_id="opencode.db_sqlite.v1",
                path=candidate,
                path_kind="sqlite_db",
                source_kind="sqlite",
                search_root=None,
                mtime_ns=file_mtime_ns(candidate),
            )
            if version_detail == "catalog":
                handle.version_detection = _catalog_version_detection(
                    descriptor,
                    descriptor.discovery[0],
                )
            elif version_detail == "shape":
                handle.version_detection = detect_source_version(
                    handle,
                    descriptor,
                    descriptor.discovery[0],
                    DiscoveryVersionContext(),
                )
            return [handle]
    base = resolve_env_root("XDG_DATA_HOME", home / ".local" / "share") / "opencode"
    if not base.exists():
        return []
    return discover_from_catalog(
        home,
        "opencode",
        base,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def _is_wsl() -> bool:
    """Detect WSL so the Windows ``/mnt/c`` VS Code data is only probed there."""
    try:
        return (
            "microsoft"
            in pathlib.Path("/proc/version").read_text(encoding="utf-8", errors="ignore").casefold()
        )
    except OSError:
        return False


_VSCODE_EDITIONS: tuple[str, ...] = (
    "Code",
    "Code - Insiders",
    "VSCodium",
    "Code - OSS",
)


def _vscode_user_dirs(home: pathlib.Path) -> tuple[pathlib.Path, ...]:
    """Return the existing VS Code ``User/`` directories across editions and OS.

    Covers the native per-platform location and — on WSL, or when
    ``VSCODE_APPDATA`` points at a Windows ``Roaming`` dir — the Windows-host
    mount, since a workspace opened through WSL persists its chat client-side
    on Windows (reachable from the distro via ``/mnt/c``). ``VSCODE_APPDATA``
    pins one ``Roaming`` dir; ``AGENTGREP_WSL_USERS_ROOT`` overrides the
    Windows users mount that the WSL auto-probe globs (default ``/mnt/c/Users``).
    """
    if sys.platform == "darwin":
        native_base = home / "Library" / "Application Support"
    elif sys.platform == "win32":
        native_base = home / "AppData" / "Roaming"
    else:
        native_base = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
    roaming_bases: list[pathlib.Path] = [native_base]
    override = os.environ.get("VSCODE_APPDATA")
    if override:
        roaming_bases.append(pathlib.Path(os.path.expandvars(override)).expanduser())
    elif _is_wsl():
        windows_users = pathlib.Path(os.environ.get("AGENTGREP_WSL_USERS_ROOT") or "/mnt/c/Users")
        if windows_users.is_dir():
            roaming_bases.extend(
                user_dir / "AppData" / "Roaming" for user_dir in sorted(windows_users.glob("*"))
            )
    user_dirs = [base / edition / "User" for base in roaming_bases for edition in _VSCODE_EDITIONS]
    return tuple(path for path in dict.fromkeys(user_dirs) if path.is_dir())


def discover_vscode_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover VS Code (GitHub Copilot Chat) sources.

    VS Code persists chat client-side in the workbench ``User/`` directory:
    per-workspace ``workspaceStorage/<hash>/chatSessions/*.json`` transcripts,
    windowless ``globalStorage/emptyWindowChatSessions/*.json``, and the global
    ``globalStorage/state.vscdb`` inline-edit history. Roots span every existing
    edition and OS ``User/`` dir (including the Windows-host mount under WSL);
    globs and adapters come from the ``vscode.*`` rows of
    :data:`agentgrep.store_catalog.CATALOG`.
    """
    user_dirs = _vscode_user_dirs(home)
    if not user_dirs:
        return []
    roots: dict[str, DiscoveryRoot] = {
        "default": home,
        "vscode_workspace": tuple(d / "workspaceStorage" for d in user_dirs),
        "vscode_global": tuple(d / "globalStorage" for d in user_dirs),
    }
    return discover_from_catalog(
        home,
        "vscode",
        roots,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )
