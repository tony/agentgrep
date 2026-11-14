"""ChatGPT desktop store descriptors for the agentgrep catalogue.

The Windows app keeps no conversation bodies on disk, but its Local Storage
caches the conversation list, which ``chatgpt.conversation_list`` searches by
title. The macOS app's conversation and draft files are encrypted with a key
only OpenAI-signed code can read, and the Windows IndexedDB holds no
conversations, so those rows are catalog-only: listed for inventory as metadata
summaries, never searched.
"""

from __future__ import annotations

from agentgrep.store_catalog._common import _CHATGPT_OBSERVED_AT
from agentgrep.stores import (
    DiscoverySpec,
    StoreCoverage,
    StoreDescriptor,
    StoreFormat,
    StoreRole,
)

_CHATGPT_OBSERVED_VERSION = "ChatGPT desktop"
"""App the ChatGPT desktop rows below were checked against.

The macOS app and the Windows MSIX package (`OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0`)
report no version in the data they write, so no build number is claimed.
"""

_MACOS_ROOT = "${HOME}/Library/Application Support/com.openai.chat"
_WINDOWS_ROOT = (
    "%LOCALAPPDATA%/Packages/OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0/LocalCache/Roaming/ChatGPT"
)


_CHATGPT_STORES: tuple[StoreDescriptor, ...] = (
    StoreDescriptor(
        agent="chatgpt",
        store_id="chatgpt.conversations",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.OPAQUE,
        path_pattern=f"{_MACOS_ROOT}/conversations-v3-<uuid>/<UUID>.data",
        platform_variants={"darwin": f"{_MACOS_ROOT}/conversations-v3-<uuid>/<UUID>.data"},
        observed_version=_CHATGPT_OBSERVED_VERSION,
        observed_at=_CHATGPT_OBSERVED_AT,
        schema_notes=(
            "macOS app, one `.data` file per conversation (898 B to 349 KB). The "
            "app (bundle `com.openai.chat`) seals each file with CryptoKit "
            "AES-GCM under a 256-bit key and stores a property list inside; the "
            "key is a data protection keychain item in OpenAI's team access "
            "group, which macOS opens only to code signed with that entitlement, "
            "so no other program can decrypt the files. Listed for inventory as "
            "metadata only; never searched."
        ),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="chatgpt.conversations",
                adapter_id="chatgpt.file_metadata_summary.v1",
                data_version="chatgpt.opaque.v1",
                path_kind="store_file",
                source_kind="opaque",
                root_key="chatgpt_macos",
                glob="conversations-v3-*/*.data",
            ),
        ),
    ),
    StoreDescriptor(
        agent="chatgpt",
        store_id="chatgpt.drafts",
        role=StoreRole.SUPPLEMENTARY_CHAT,
        format=StoreFormat.OPAQUE,
        path_pattern=f"{_MACOS_ROOT}/drafts-v2-<uuid>/<UUID>.data",
        platform_variants={"darwin": f"{_MACOS_ROOT}/drafts-v2-<uuid>/<UUID>.data"},
        observed_version=_CHATGPT_OBSERVED_VERSION,
        observed_at=_CHATGPT_OBSERVED_AT,
        schema_notes=(
            "macOS app, unsent prompt drafts (183 B to 1.8 KB), encrypted like "
            "the conversation files. Listed for inventory as metadata only; never searched."
        ),
        distinguishes_from=("chatgpt.conversations",),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="chatgpt.drafts",
                adapter_id="chatgpt.file_metadata_summary.v1",
                data_version="chatgpt.opaque.v1",
                path_kind="store_file",
                source_kind="opaque",
                root_key="chatgpt_macos",
                glob="drafts-v2-*/*.data",
            ),
        ),
    ),
    StoreDescriptor(
        agent="chatgpt",
        store_id="chatgpt.conversation_list",
        role=StoreRole.APP_STATE,
        format=StoreFormat.LEVELDB,
        path_pattern=f"{_WINDOWS_ROOT}/Local Storage/leveldb/",
        platform_variants={"win32": f"{_WINDOWS_ROOT}/Local Storage/leveldb/"},
        observed_version=_CHATGPT_OBSERVED_VERSION,
        observed_at=_CHATGPT_OBSERVED_AT,
        schema_notes=(
            "Windows app, Chromium Local Storage in LevelDB (`*.log` write-ahead "
            "logs and Snappy-compressed `*.ldb` tables). Keys "
            "`cache/user-<account>/<id>/conversation-history` and "
            "`.../conversation-history-without-projects` hold the cached first "
            "page of the conversation list as JSON: "
            "`{timestamp, value: {pages: [{items, total, limit, offset}]}, version}`, "
            "each item with `id`, `title`, `create_time` and `update_time` (ISO "
            "8601), `gizmo_id` (project), and archive, star, and temporary flags; "
            "`mapping` and `snippet` are always null, so the list carries titles, "
            "not messages. The same store holds session data and secrets under "
            "other keys, which are never decoded."
        ),
        distinguishes_from=("chatgpt.web_profile",),
        coverage=StoreCoverage.DEFAULT_SEARCH,
        search_by_default=False,
        search_notes=(
            "One title record per conversation, newest snapshot per id; its "
            "session id is the ChatGPT conversation id. Exhaustive or broad-scope "
            "search reads it; prompt search does not."
        ),
        discovery=(
            DiscoverySpec(
                store="chatgpt.conversation_list",
                adapter_id="chatgpt.local_storage_conversation_list.v1",
                data_version="chatgpt.local_storage_conversation_list.v1",
                path_kind="store_file",
                source_kind="opaque",
                root_key="chatgpt_windows",
                files=("Local Storage/leveldb/CURRENT",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="chatgpt",
        store_id="chatgpt.web_profile",
        role=StoreRole.CACHE,
        format=StoreFormat.OPAQUE,
        path_pattern=f"{_WINDOWS_ROOT}/IndexedDB/https_chatgpt.com_0.indexeddb.leveldb/",
        platform_variants={
            "win32": f"{_WINDOWS_ROOT}/IndexedDB/https_chatgpt.com_0.indexeddb.leveldb/",
        },
        observed_version=_CHATGPT_OBSERVED_VERSION,
        observed_at=_CHATGPT_OBSERVED_AT,
        schema_notes=(
            "Windows app, an Electron shell around chatgpt.com installed as an "
            "MSIX package. Windows redirects its `%APPDATA%` writes to the "
            "package's `LocalCache/Roaming`, so from WSL the data is only under "
            "`%LOCALAPPDATA%/Packages`. The directory is a Chromium profile "
            "(`IndexedDB`, `Local Storage`, `Session Storage`, `Cache`, "
            "`Preferences`, a two-byte `config.json`). Decoded, the IndexedDB "
            "`ConversationsDatabase.conversations` store holds no records and no "
            "conversation body response is cached, so conversation text is held "
            "server-side; the conversation list is read from Local Storage "
            "(`chatgpt.conversation_list`). Listed for inventory as metadata only; "
            "never searched."
        ),
        distinguishes_from=("chatgpt.conversations", "chatgpt.conversation_list"),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="chatgpt.web_profile",
                adapter_id="chatgpt.file_metadata_summary.v1",
                data_version="chatgpt.opaque.v1",
                path_kind="store_file",
                source_kind="opaque",
                root_key="chatgpt_windows",
                glob="IndexedDB/https_chatgpt.com_0.indexeddb.leveldb/*",
            ),
        ),
    ),
)
