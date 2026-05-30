"""CSS class constants for the storage documentation extension."""

from __future__ import annotations


class StorageCSS:
    """Class names in the ``gp-sphinx-storage`` namespace."""

    PREFIX = "gp-sphinx-storage"

    STORE_SECTION = "gp-sphinx-storage__store-section"
    STORE_ENTRY = "gp-sphinx-storage__store-entry"
    STORE_SIGNATURE = "gp-sphinx-storage__store-signature"
    BODY_SECTION = "gp-sphinx-storage__body-section"
    SECTION_TITLE_HIDDEN = "gp-sphinx-storage__visually-hidden"

    STORE_INDEX = "gp-sphinx-storage__store-index"
    STORE_INDEX_CARD = "gp-sphinx-storage__store-index-card"
    SUPPORT_MATRIX = "gp-sphinx-storage__support-matrix"
    SUPPORT_AGENT_CARD = "gp-sphinx-storage__support-agent-card"
    CATALOG_SUMMARY = "gp-sphinx-storage__catalog-summary"
    CATALOG_SUMMARY_CARD = "gp-sphinx-storage__catalog-summary-card"
    KEY_VALUE = "gp-sphinx-storage__key-value"
    CHIP_LIST = "gp-sphinx-storage__chip-list"
    EMPTY_VALUE = "gp-sphinx-storage__empty-value"

    BADGE_COVERAGE = "gp-sphinx-storage__coverage"
    BADGE_TYPE = "gp-sphinx-storage__type"
    BADGE_TAXONOMY = "gp-sphinx-storage__taxonomy"
    TYPE_STORE = "gp-sphinx-storage__type-store"

    TABLE = "gp-sphinx-storage__table"
    GRID_TABLE = "gp-sphinx-storage__coverage-grid"
    SUMMARY_TABLE = "gp-sphinx-storage__summary-table"

    @staticmethod
    def coverage_class(value: str) -> str:
        """Return the CSS modifier class for a coverage value."""
        return f"gp-sphinx-storage__coverage-{value.replace('_', '-')}"
