"""ChatGPT desktop store descriptors for the agentgrep catalogue.

Every row is catalog-only: the macOS app's conversation files look encrypted
and the Windows app keeps only a Chromium profile, so agentgrep lists the files
for inventory (as metadata summaries) and never searches them.
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
            "macOS app, one `.data` file per conversation (898 B to 349 KB). None "
            "of 30 sampled files parsed as JSON, UTF-8, zlib, raw deflate, gzip, "
            "bzip2, or lzma; entropy of the first 64 KiB was 7.76 to 8.00 bits per "
            "byte and every file started with different bytes, which fits "
            "authenticated encryption. Listed for inventory as metadata only; never searched."
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
            "macOS app, unsent prompt drafts (183 B to 1.8 KB), as opaque as the "
            "conversation files. Listed for inventory as metadata only; never searched."
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
            "(`IndexedDB`, `Local Storage`, `Session Storage`, `Preferences`, a "
            "two-byte `config.json`); searching the IndexedDB files as bytes for "
            '`"author"` or `conversation_id` found nothing, so conversations '
            "are held server-side. Listed for inventory as metadata only; never searched."
        ),
        distinguishes_from=("chatgpt.conversations",),
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
