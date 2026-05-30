"""Cross-reference roles for storage documentation."""

from __future__ import annotations

import typing as t

from docutils import nodes
from sphinx.roles import XRefRole

if t.TYPE_CHECKING:
    from docutils.nodes import Element
    from sphinx.environment import BuildEnvironment


class StorageStoreXRefRole(XRefRole):
    """Cross-reference role that asks the storage domain to add store badges."""

    def __init__(self, *, show_badge: bool, icon_pos: str = "") -> None:
        """Initialize the role with display options consumed during resolution."""
        super().__init__(innernodeclass=nodes.literal, warn_dangling=True)
        self.show_badge = show_badge
        self.icon_pos = icon_pos

    def process_link(
        self,
        env: BuildEnvironment,
        refnode: Element,
        has_explicit_title: bool,
        title: str,
        target: str,
    ) -> tuple[str, str]:
        """Normalize store IDs and attach badge display metadata."""
        clean_target = target.strip()
        if not has_explicit_title:
            title = clean_target
        refnode["storage_show_badge"] = self.show_badge
        refnode["storage_icon_pos"] = self.icon_pos
        return title, clean_target
