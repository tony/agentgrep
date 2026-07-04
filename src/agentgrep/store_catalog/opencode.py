"""opencode store descriptors for the agentgrep catalogue."""

from __future__ import annotations

from agentgrep.store_catalog._common import _OPENCODE_OBSERVED_AT
from agentgrep.stores import (
    DiscoverySpec,
    StoreCoverage,
    StoreDescriptor,
    StoreFormat,
    StoreRole,
)

_OPENCODE_STORES: tuple[StoreDescriptor, ...] = (
    StoreDescriptor(
        agent="opencode",
        store_id="opencode.db",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.SQLITE,
        path_pattern="${XDG_DATA_HOME or ${HOME}/.local/share}/opencode/opencode.db",
        env_overrides=("XDG_DATA_HOME", "OPENCODE_DB"),
        observed_version="opencode v1.17.9 (observed 2026-06-21)",
        observed_at=_OPENCODE_OBSERVED_AT,
        upstream_ref=(
            "github.com/anomalyco/opencode/blob/v1.17.9/packages/core/src/session/sql.ts#L23-L82"
        ),
        schema_notes=(
            "SQLite store (Drizzle). Tables `session` (id, project_id, "
            "`directory` = working dir, title, version, time_created/updated, "
            "model, cost, tokens_*), `message` (id, session_id FK, `data` JSON "
            "with `role`/`time`; assistant messages carry top-level "
            "`modelID`/`providerID`/`path.cwd`, while user messages nest the "
            "selected model under `model.modelID`), and "
            "`part` (id, message_id FK, session_id, `data` JSON with type + "
            "payload). Searchable text lives in `part.data`: type `text`/"
            "`reasoning` -> `text`, `subtask` -> `prompt`. A conversation turn "
            "is reconstructed by joining part -> message -> session. Channel "
            "installs use `opencode-<channel>.db`; `OPENCODE_DB` overrides the "
            "path (also `:memory:`/absolute). The same file also carries the "
            "v2 event-sourced tables `session_input`, `session_message`, "
            "`event`/`event_sequence`, and `todo`; `event` is now populated on "
            "stable installs and mirrors the `part` transcript text, so it is "
            "left unsearched to avoid duplicate hits — the canonical transcript "
            "stays in `session`/`message`/`part`. "
            "Secret-bearing `account`, `account_state`, `control_account`, and "
            "`credential` tables are present but never enumerated — the adapter "
            "reads only text-bearing `part` rows."
        ),
        sample_record=(
            'part.data: {"type":"text","text":"<redacted>",'
            '"time":{"start":1779999665000,"end":1779999666000}}'
        ),
        search_by_default=True,
        search_notes=(
            "The sole searchable OpenCode store. kind is derived from the "
            "joined message role (user -> prompt, else history)."
        ),
        discovery=(
            DiscoverySpec(
                store="opencode.db",
                adapter_id="opencode.db_sqlite.v1",
                path_kind="sqlite_db",
                source_kind="sqlite",
                root_key="default",
                files=("opencode.db",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="opencode",
        store_id="opencode.storage_legacy",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.JSON_OBJECT,
        path_pattern=(
            "${XDG_DATA_HOME or ${HOME}/.local/share}/opencode/storage/"
            "{session,message,part}/**/*.json"
        ),
        env_overrides=("XDG_DATA_HOME",),
        observed_version="opencode v1.17.9 (observed 2026-06-21)",
        observed_at=_OPENCODE_OBSERVED_AT,
        upstream_ref=(
            "github.com/anomalyco/opencode/blob/v1.17.9/packages/opencode/"
            "src/storage/storage.ts#L189-L230"
        ),
        schema_notes=(
            "Pre-migration on-disk layout: one JSON file per session/message/"
            "part. A startup migration folds these into opencode.db; migrated "
            "installs keep a `storage/session_diff/` of small per-session `[]` "
            "marker files and a `storage/migration` marker, with no searchable "
            "session/message/part JSON left. Documentary — relevant only to "
            "older, un-migrated installs."
        ),
        distinguishes_from=("opencode.db",),
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="opencode",
        store_id="opencode.config",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${XDG_CONFIG_HOME or ${HOME}/.config}/opencode/opencode.{json,jsonc}",
        env_overrides=("XDG_CONFIG_HOME", "OPENCODE_CONFIG_DIR"),
        observed_version="opencode v1.17.9 (observed 2026-06-21)",
        observed_at=_OPENCODE_OBSERVED_AT,
        schema_notes=(
            "Application config (`opencode.json`/`opencode.jsonc`): providers, "
            "agents, plugins, commands, UI settings. Configuration, not chat."
        ),
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="opencode",
        store_id="opencode.auth",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${XDG_DATA_HOME or ${HOME}/.local/share}/opencode/auth.json",
        env_overrides=("XDG_DATA_HOME",),
        observed_version="opencode v1.17.9 (observed 2026-06-21)",
        observed_at=_OPENCODE_OBSERVED_AT,
        schema_notes="Provider API keys and OAuth tokens. Documented but never enumerated.",
        coverage=StoreCoverage.PRIVATE,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="opencode",
        store_id="opencode.snapshots",
        role=StoreRole.SOURCE_TREE,
        format=StoreFormat.OPAQUE,
        path_pattern="${XDG_DATA_HOME or ${HOME}/.local/share}/opencode/snapshot/",
        env_overrides=("XDG_DATA_HOME",),
        observed_version="opencode v1.17.9 (observed 2026-06-21)",
        observed_at=_OPENCODE_OBSERVED_AT,
        schema_notes="Per-project git repositories holding session file snapshots.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="opencode",
        store_id="opencode.repos",
        role=StoreRole.CACHE,
        format=StoreFormat.OPAQUE,
        path_pattern="${XDG_DATA_HOME or ${HOME}/.local/share}/opencode/repos/",
        env_overrides=("XDG_DATA_HOME",),
        observed_version="opencode v1.17.9 (observed 2026-06-21)",
        observed_at=_OPENCODE_OBSERVED_AT,
        schema_notes="Cache of cloned git repositories referenced during sessions.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="opencode",
        store_id="opencode.logs",
        role=StoreRole.APP_STATE,
        format=StoreFormat.TEXT,
        path_pattern="${XDG_DATA_HOME or ${HOME}/.local/share}/opencode/log/",
        env_overrides=("XDG_DATA_HOME",),
        observed_version="opencode v1.17.9 (observed 2026-06-21)",
        observed_at=_OPENCODE_OBSERVED_AT,
        schema_notes="Timestamped application logs. Diagnostics, not chat content.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="opencode",
        store_id="opencode.tool_output",
        role=StoreRole.CACHE,
        format=StoreFormat.TEXT,
        path_pattern="${XDG_DATA_HOME or ${HOME}/.local/share}/opencode/tool-output/",
        env_overrides=("XDG_DATA_HOME",),
        observed_version="opencode v1.17.9 (observed 2026-06-21)",
        observed_at=_OPENCODE_OBSERVED_AT,
        schema_notes="Overflow storage for large tool output that exceeds inline limits.",
        search_by_default=False,
    ),
)
