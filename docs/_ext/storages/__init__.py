"""Sphinx extension for generated agentgrep storage documentation."""

from __future__ import annotations

import pathlib
import typing as t

from ._directives import (
    StorageAgentDirective,
    StorageCatalogSummaryDirective,
    StorageCoverageGridDirective,
    StorageStoreDirective,
)
from ._domain import StorageDomain

if t.TYPE_CHECKING:
    from sphinx.application import Sphinx

__version__ = "0.1.0"

__all__ = [
    "StorageAgentDirective",
    "StorageCatalogSummaryDirective",
    "StorageCoverageGridDirective",
    "StorageDomain",
    "StorageStoreDirective",
    "__version__",
    "setup",
]

_STATIC_DIR = str(pathlib.Path(__file__).parent / "_static")


def _add_static_path(app: Sphinx) -> None:
    """Add the extension's static directory to the Sphinx build."""
    if _STATIC_DIR not in app.config.html_static_path:
        app.config.html_static_path.append(_STATIC_DIR)


def setup(app: Sphinx) -> dict[str, t.Any]:
    """Register storage-domain roles, directives, and static assets."""
    app.setup_extension("sphinx_ux_badges")
    app.setup_extension("sphinx_ux_autodoc_layout")

    app.add_config_value(
        "storage_catalog_object",
        default="agentgrep.store_catalog:CATALOG",
        rebuild="env",
        types=(str,),
        description="Python object path for the StoreCatalog used by storage directives.",
    )

    app.add_domain(StorageDomain)
    app.add_directive_to_domain("storage", "agent", StorageAgentDirective)
    app.add_directive_to_domain("storage", "store", StorageStoreDirective)
    app.add_directive_to_domain(
        "storage",
        "catalog-summary",
        StorageCatalogSummaryDirective,
    )
    app.add_directive_to_domain(
        "storage",
        "coverage-grid",
        StorageCoverageGridDirective,
    )

    app.connect("builder-inited", _add_static_path)
    app.add_css_file("css/storage.css")

    return {
        "version": __version__,
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }
