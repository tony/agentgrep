"""Sphinx extension for generated agentgrep storage documentation."""

from __future__ import annotations

import pathlib
import typing as t

from sphinx.util import logging

from ._directives import (
    StorageAgentDirective,
    StorageCatalogSummaryDirective,
    StorageCoverageGridDirective,
    StorageStoreDirective,
    _load_catalog,
)
from ._domain import StorageDomain
from ._observations import detect_version_drift, load_observation_index

if t.TYPE_CHECKING:
    from collections.abc import Sequence

    from sphinx.application import Sphinx
    from sphinx.environment import BuildEnvironment

    from agentgrep.stores import StoreCatalog

logger = logging.getLogger(__name__)

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


def _observations_root(app: Sphinx) -> pathlib.Path:
    """Return the manifest directory, resolved against confdir."""
    configured = str(app.config.storage_observations_dir).strip() or "_observations"
    return pathlib.Path(app.confdir) / configured


def _catalog_observed_versions(catalog: StoreCatalog) -> dict[str, tuple[str, ...]]:
    """Return the distinct ``observed_version`` stamps the catalog holds per agent."""
    stamps: dict[str, dict[str, None]] = {}
    for store in catalog.stores:
        stamps.setdefault(store.agent, {})[store.observed_version] = None
    return {agent: tuple(values) for agent, values in stamps.items()}


def _index_observations(app: Sphinx) -> None:
    """Read every manifest once, then report drift once for the whole build.

    Running here rather than per directive means a store card is a dict lookup,
    and an agent whose stamp has moved on warns once instead of once per card.
    """
    index = load_observation_index(_observations_root(app))
    _storage_domain(app.env).observations = index

    for problem in index.problems:
        logger.warning(
            "storage observations: skipped manifest %s",
            problem,
            type="storage",
            subtype="observation-manifest",
        )

    if not index.agents:
        return

    catalog = _load_catalog(app.config)
    for drift in detect_version_drift(index, _catalog_observed_versions(catalog)):
        logger.warning("%s", drift.message, type="storage", subtype="observation-drift")


def _outdated_observation_docs(
    app: Sphinx,
    env: BuildEnvironment,
    added: set[str],
    changed: set[str],
    removed: set[str],
) -> Sequence[str]:
    """Re-read storage pages when a manifest appeared that none of them read.

    Every storage page depends on every manifest, so Sphinx already catches an
    edit or a deletion. A brand-new file is the one case it cannot see: it was
    not a dependency of anything, and adding it can change which manifest is
    newest.
    """
    del app, added, changed, removed
    domain = _storage_domain(env)
    on_disk = {str(path) for path in domain.observations.paths}
    if not on_disk:
        return ()

    stale: list[str] = []
    for docname in {record["docname"] for record in domain.objects.values()}:
        known = {str(env.srcdir / dep) for dep in env.dependencies.get(docname, ())}
        if on_disk - known:
            stale.append(docname)
    return sorted(stale)


def _storage_domain(env: BuildEnvironment) -> StorageDomain:
    """Return the storage domain attached to *env*."""
    return t.cast("StorageDomain", env.get_domain("storage"))


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
    app.add_config_value(
        "storage_observations_dir",
        default="",
        rebuild="env",
        types=(str,),
        description=("Directory of observation manifests, relative to confdir."),
    )

    app.add_domain(StorageDomain)
    app.add_directive_to_domain("storage", "agent", StorageAgentDirective)
    app.add_directive_to_domain("storage", "store", StorageStoreDirective)
    app.add_directive_to_domain("storage", "catalog-summary", StorageCatalogSummaryDirective)
    app.add_directive_to_domain("storage", "coverage-grid", StorageCoverageGridDirective)

    app.connect("builder-inited", _add_static_path)
    app.connect("builder-inited", _index_observations)
    app.connect("env-get-outdated", _outdated_observation_docs)
    app.add_css_file("css/storage.css")

    return {
        "version": __version__,
        # The index is built on builder-inited, before Sphinx forks its read
        # workers, and is never written back.
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }
