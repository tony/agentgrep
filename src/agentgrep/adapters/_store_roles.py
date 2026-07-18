"""Catalog store-role lookups for normalized records.

Engine-facing helpers, not parsing: they map a record's
``store``/``adapter_id`` pair back to its catalogue descriptor. The
``CATALOG`` import stays lazy (inside the cached lookup) because the
store catalogue is the one downstream dependency of this package.
"""

from __future__ import annotations

import functools

from agentgrep.records import (
    CONVERSATION_STORE_ROLES,
    PROMPT_HISTORY_STORE_ROLES,
    DiscoveryStoreRoles,
    FindSourceTypeFilter,
)
from agentgrep.stores import StoreDescriptor, StoreRole


def find_store_roles_for_type_filter(
    type_filter: FindSourceTypeFilter,
) -> DiscoveryStoreRoles:
    """Return catalogue roles that can satisfy a ``find --type`` filter."""
    if type_filter in {"prompts", "history"}:
        return PROMPT_HISTORY_STORE_ROLES
    if type_filter == "sessions":
        return CONVERSATION_STORE_ROLES
    return None


@functools.cache
def store_descriptor_for_record(store: str, adapter_id: str) -> StoreDescriptor | None:
    """Return the catalog descriptor for a normalized record's source store."""
    from agentgrep.store_catalog import CATALOG

    for descriptor in CATALOG.stores:
        for spec in descriptor.discovery:
            if spec.store == store and spec.adapter_id == adapter_id:
                return descriptor
    return None


def store_role_for_record(store: str, adapter_id: str) -> StoreRole | None:
    """Return the catalog role for a normalized record's source store."""
    descriptor = store_descriptor_for_record(store, adapter_id)
    if descriptor is None:
        return None
    return descriptor.role
