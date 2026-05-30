"""Badge helpers for storage documentation."""

from __future__ import annotations

from docutils import nodes
from sphinx_ux_badges import SAB, BadgeNode, BadgeSpec, build_badge, build_badge_group_from_specs

from ._css import StorageCSS
from ._utils import coverage_label

_COVERAGE_TOOLTIPS: dict[str, str] = {
    "default_search": "Default search store",
    "inspectable": "Inspectable opt-in store",
    "catalog_only": "Catalog-only store",
    "private": "Private store, intentionally not enumerated",
}


def build_coverage_badge(value: str, *, icon_only: bool = False) -> BadgeNode:
    """Build a badge for one storage coverage level."""
    return build_badge(
        "" if icon_only else coverage_label(value),
        tooltip=_COVERAGE_TOOLTIPS.get(value, f"Storage coverage: {value}"),
        classes=[
            SAB.DENSE,
            SAB.NO_UNDERLINE,
            StorageCSS.BADGE_COVERAGE,
            StorageCSS.coverage_class(value),
        ],
        style="icon-only" if icon_only else "full",
    )


def build_store_type_badge() -> BadgeNode:
    """Build the rightmost type badge for a storage entry."""
    return build_badge(
        "store",
        tooltip="agentgrep storage descriptor",
        classes=[
            SAB.DENSE,
            SAB.NO_UNDERLINE,
            SAB.BADGE_TYPE,
            StorageCSS.BADGE_TYPE,
            StorageCSS.TYPE_STORE,
        ],
    )


def build_store_badge_group(coverage: str) -> nodes.inline:
    """Build coverage + type badges for one store."""
    return build_badge_group_from_specs(
        [
            BadgeSpec(
                coverage_label(coverage),
                tooltip=_COVERAGE_TOOLTIPS.get(coverage, f"Storage coverage: {coverage}"),
                classes=(
                    SAB.DENSE,
                    SAB.NO_UNDERLINE,
                    StorageCSS.BADGE_COVERAGE,
                    StorageCSS.coverage_class(coverage),
                ),
            ),
            BadgeSpec(
                "store",
                tooltip="agentgrep storage descriptor",
                classes=(
                    SAB.DENSE,
                    SAB.NO_UNDERLINE,
                    SAB.BADGE_TYPE,
                    StorageCSS.BADGE_TYPE,
                    StorageCSS.TYPE_STORE,
                ),
            ),
        ],
    )


def build_taxonomy_badge(value: str, *, tooltip: str = "") -> BadgeNode:
    """Build a neutral badge for a storage taxonomy value."""
    return build_badge(
        value,
        tooltip=tooltip or f"Storage taxonomy: {value}",
        classes=[SAB.DENSE, SAB.NO_UNDERLINE, StorageCSS.BADGE_TAXONOMY],
    )
