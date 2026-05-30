"""Sphinx directives that render storage catalog documentation."""

from __future__ import annotations

import importlib
import typing as t

from docutils import nodes
from sphinx.util.docutils import SphinxDirective
from sphinx_ux_autodoc_layout import (
    API,
    ApiFactRow,
    api_permalink,
    build_api_card_entry,
    build_api_facts_section,
    build_api_section,
    build_api_summary_section,
)

from ._badges import build_store_badge_group
from ._css import StorageCSS
from ._domain import StorageDomain
from ._utils import (
    comma_literal_list,
    display_value,
    literal_paragraph,
    make_table,
    store_adapter_ids,
    store_data_versions,
    store_strategy_values,
    store_target_id,
    text_paragraph,
)

if t.TYPE_CHECKING:
    from collections.abc import Sequence

    from agentgrep.stores import StoreCatalog, StoreDescriptor

_AGENT_LABELS: dict[str, str] = {
    "claude": "Claude",
    "codex": "Codex",
    "cursor": "Cursor",
    "gemini": "Gemini",
    "grok": "Grok",
}


def _load_catalog(config: t.Any) -> StoreCatalog:
    """Load the configured catalog object."""
    object_path = str(config.storage_catalog_object)
    module_name, separator, attr_name = object_path.partition(":")
    if not separator or not attr_name:
        msg = "storage_catalog_object must use 'module:attribute' syntax"
        raise ValueError(msg)
    module = importlib.import_module(module_name)
    return t.cast("StoreCatalog", getattr(module, attr_name))


def _storage_domain(directive: SphinxDirective) -> StorageDomain:
    """Return the active storage domain for *directive*."""
    return t.cast(StorageDomain, directive.env.get_domain("storage"))


def _store_link(store: StoreDescriptor, *, docname: str = "") -> nodes.paragraph:
    """Return a paragraph linking to one store anchor."""
    paragraph = nodes.paragraph()
    ref = nodes.reference("", "", internal=True)
    if docname:
        ref["refuri"] = f"../{docname}/#{store_target_id(store.store_id)}"
    else:
        ref["refuri"] = f"#{store_target_id(store.store_id)}"
    ref += nodes.literal("", store.store_id)
    paragraph += ref
    return paragraph


def _store_literals(stores: Sequence[StoreDescriptor]) -> nodes.paragraph:
    """Return a paragraph containing comma-separated store IDs."""
    return comma_literal_list([store.store_id for store in stores])


def _adapter_literal(store: StoreDescriptor) -> nodes.paragraph:
    """Return a paragraph containing adapter ids or a dash."""
    adapters = store_adapter_ids(store)
    return literal_paragraph(adapters) if adapters else text_paragraph("-")


def _store_card(directive: SphinxDirective, store: StoreDescriptor) -> nodes.section:
    """Build one gp-sphinx card for a storage descriptor."""
    node_id = store_target_id(store.store_id)
    section = nodes.section(ids=[node_id])
    section["classes"].extend((StorageCSS.STORE_SECTION, API.CARD_SHELL))

    _storage_domain(directive).note_object(
        "store",
        store.store_id,
        node_id,
        title=store.store_id,
        coverage=store.coverage_level.value,
    )
    directive.state.document.note_explicit_target(section)

    title_node = nodes.title("", "")
    title_node["classes"].append(StorageCSS.SECTION_TITLE_HIDDEN)
    title_node += nodes.literal("", store.store_id)
    section += title_node

    permalink = api_permalink(href=f"#{node_id}", title="Link to this store")
    permalink["classes"] = ["headerlink", API.LINK]

    facts = [
        ApiFactRow("Agent", nodes.paragraph("", _AGENT_LABELS.get(store.agent, store.agent))),
        ApiFactRow("Role", literal_paragraph(store.role.value)),
        ApiFactRow("Format", literal_paragraph(store.format.value)),
        ApiFactRow("Coverage", literal_paragraph(store.coverage_level.value)),
        ApiFactRow("Path", literal_paragraph(store.path_pattern)),
        ApiFactRow("Adapter", _adapter_literal(store)),
        ApiFactRow("Data version", literal_paragraph(store_data_versions(store) or "-")),
        ApiFactRow("Version strategies", literal_paragraph(store_strategy_values(store) or "-")),
        ApiFactRow(
            "Observed", nodes.paragraph("", f"{store.observed_version} ({store.observed_at})")
        ),
        ApiFactRow(
            "Default search",
            nodes.paragraph("", "yes" if store.search_by_default is True else "no"),
        ),
    ]

    content_nodes: list[nodes.Node] = [
        build_api_section(
            API.DESCRIPTION,
            text_paragraph(store.schema_notes),
            classes=(StorageCSS.BODY_SECTION,),
        ),
        build_api_facts_section(facts, classes=(StorageCSS.BODY_SECTION,)),
    ]
    if store.search_notes:
        content_nodes.append(
            build_api_section(
                API.DESCRIPTION,
                text_paragraph(store.search_notes),
                classes=(StorageCSS.BODY_SECTION,),
            ),
        )

    section += build_api_card_entry(
        profile_class=API.profile("storage-store"),
        signature_children=(nodes.literal("", store.store_id),),
        content_children=tuple(content_nodes),
        badge_group=build_store_badge_group(store.coverage_level.value),
        permalink=permalink,
        entry_classes=(StorageCSS.STORE_ENTRY,),
        signature_classes=(StorageCSS.STORE_SIGNATURE,),
    )
    return section


def _agent_stores(catalog: StoreCatalog, agent: str) -> tuple[StoreDescriptor, ...]:
    """Return catalog stores for an agent, validating the name."""
    known_agents = {store.agent for store in catalog.stores}
    if agent not in known_agents:
        msg = f"unknown storage agent: {agent}"
        raise ValueError(msg)
    return tuple(store for store in catalog.stores if store.agent == agent)


def _agent_summary_table(stores: Sequence[StoreDescriptor]) -> nodes.table:
    """Build a compact table for one backend's stores."""
    rows: list[list[str | nodes.Node]] = [
        [
            _store_link(store),
            display_value(store.role),
            display_value(store.format),
            display_value(store.coverage_level),
            store_adapter_ids(store) or "-",
        ]
        for store in stores
    ]
    return make_table(
        ["Store ID", "Role", "Format", "Coverage", "Adapter ID"],
        rows,
        classes=(StorageCSS.TABLE, StorageCSS.SUMMARY_TABLE),
    )


def _stores_matching(
    stores: Sequence[StoreDescriptor],
    predicate: t.Callable[[StoreDescriptor], bool],
) -> tuple[StoreDescriptor, ...]:
    """Return stores accepted by *predicate*."""
    return tuple(store for store in stores if predicate(store))


def _coverage_grid_rows(catalog: StoreCatalog) -> list[list[str | nodes.Node]]:
    """Build support-grid rows grouped by agent."""
    rows: list[list[str | nodes.Node]] = []
    agents = sorted({store.agent for store in catalog.stores})
    for agent in agents:
        stores = tuple(store for store in catalog.stores if store.agent == agent)
        default_stores = _stores_matching(
            stores,
            lambda store: store.coverage_level.value == "default_search",
        )
        inspectable_stores = _stores_matching(
            stores,
            lambda store: store.coverage_level.value == "inspectable",
        )
        catalog_stores = _stores_matching(
            stores,
            lambda store: store.coverage_level.value == "catalog_only",
        )
        memory_stores = _stores_matching(
            stores,
            lambda store: store.role.value == "persistent_memory",
        )
        planning_stores = _stores_matching(
            stores,
            lambda store: store.role.value in {"plan", "todo"} or "goal" in store.store_id,
        )
        instruction_stores = _stores_matching(
            stores,
            lambda store: (
                store.role.value == "instruction"
                or any(part in store.store_id for part in ("plugin", "skill", "rule", "command"))
            ),
        )
        index_stores = _stores_matching(
            stores,
            lambda store: any(part in store.store_id for part in ("index", "summary", "tracking")),
        )
        app_state_stores = _stores_matching(
            stores,
            lambda store: (
                store.role.value == "app_state"
                or any(part in store.store_id for part in ("config", "state", "model"))
            ),
        )
        raw_stores = _stores_matching(
            stores,
            lambda store: (
                store.coverage_level.value == "private"
                or store.role.value == "cache"
                or any(
                    part in store.store_id
                    for part in (
                        "auth",
                        "cache",
                        "debug",
                        "log",
                        "runtime",
                        "secret",
                        "snapshot",
                        "sidecar",
                        "upload",
                        "worktree",
                    )
                )
            ),
        )
        rows.append(
            [
                _AGENT_LABELS.get(agent, agent.title()),
                _store_literals(default_stores),
                _store_literals(inspectable_stores),
                _store_literals(catalog_stores),
                _store_literals(memory_stores),
                _store_literals(planning_stores),
                _store_literals(instruction_stores),
                _store_literals(index_stores),
                _store_literals(app_state_stores),
                _store_literals(raw_stores),
            ],
        )
    return rows


class StorageAgentDirective(SphinxDirective):
    """Render storage documentation for one agent."""

    required_arguments = 1
    optional_arguments = 0
    has_content = False
    final_argument_whitespace = False

    def run(self) -> list[nodes.Node]:
        """Render one backend's generated store summary and cards."""
        catalog = _load_catalog(self.config)
        agent = self.arguments[0].strip()
        try:
            stores = _agent_stores(catalog, agent)
        except ValueError as exc:
            return [self.state.document.reporter.warning(str(exc), line=self.lineno)]

        domain = _storage_domain(self)
        domain.note_object(
            "agent",
            agent,
            f"storage-agent-{agent}",
            title=_AGENT_LABELS.get(agent, agent),
        )

        result: list[nodes.Node] = [
            build_api_summary_section(
                _agent_summary_table(stores),
                classes=(StorageCSS.BODY_SECTION,),
            ),
        ]
        result.extend(_store_card(self, store) for store in stores)
        return result


class StorageStoreDirective(SphinxDirective):
    """Render one storage descriptor by store id."""

    required_arguments = 1
    optional_arguments = 0
    has_content = False
    final_argument_whitespace = False

    def run(self) -> list[nodes.Node]:
        """Render one store card."""
        catalog = _load_catalog(self.config)
        store_id = self.arguments[0].strip()
        try:
            store = catalog.by_id(store_id)
        except KeyError:
            return [
                self.state.document.reporter.warning(
                    f"storage:store: unknown store '{store_id}'",
                    line=self.lineno,
                ),
            ]
        return [_store_card(self, store)]


class StorageCatalogSummaryDirective(SphinxDirective):
    """Render high-level storage catalog counts."""

    required_arguments = 0
    optional_arguments = 0
    has_content = False

    def run(self) -> list[nodes.Node]:
        """Render summary count tables for the configured catalog."""
        catalog = _load_catalog(self.config)
        by_agent: dict[str, int] = {}
        by_coverage: dict[str, int] = {}
        by_role: dict[str, int] = {}
        by_format: dict[str, int] = {}
        for store in catalog.stores:
            by_agent[store.agent] = by_agent.get(store.agent, 0) + 1
            by_coverage[store.coverage_level.value] = (
                by_coverage.get(store.coverage_level.value, 0) + 1
            )
            by_role[store.role.value] = by_role.get(store.role.value, 0) + 1
            by_format[store.format.value] = by_format.get(store.format.value, 0) + 1

        tables = [
            ("By agent", by_agent),
            ("By coverage", by_coverage),
            ("By role", by_role),
            ("By format", by_format),
        ]
        result: list[nodes.Node] = []
        for title, counts in tables:
            result.append(nodes.rubric("", title))
            rows = [[key, str(value)] for key, value in sorted(counts.items())]
            result.append(
                make_table(
                    ["Value", "Stores"],
                    rows,
                    classes=(StorageCSS.TABLE, StorageCSS.SUMMARY_TABLE),
                )
            )
        return result


class StorageCoverageGridDirective(SphinxDirective):
    """Render the backend support grid from the storage catalog."""

    required_arguments = 0
    optional_arguments = 0
    has_content = False

    def run(self) -> list[nodes.Node]:
        """Render the generated backend coverage grid."""
        catalog = _load_catalog(self.config)
        return [
            make_table(
                [
                    "Agent",
                    "Default search",
                    "Opt-in parsers",
                    "Safe catalog samples",
                    "Memory",
                    "Plans / todos / goals",
                    "Instructions / plugins / skills",
                    "Indexes / summaries",
                    "App state / config",
                    "Runtime / cache / private",
                ],
                _coverage_grid_rows(catalog),
                classes=(StorageCSS.TABLE, StorageCSS.GRID_TABLE),
            )
        ]
