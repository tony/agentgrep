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
    literal_paragraph,
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


_SUPPORT_GROUP_LABELS = (
    "Default search",
    "Opt-in parsers",
    "Safe catalog samples",
    "Memory",
    "Plans / todos / goals",
    "Instructions / plugins / skills",
    "Indexes / summaries",
    "App state / config",
    "Runtime / cache / private",
)


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


def _store_reference(store: StoreDescriptor, *, docname: str = "") -> nodes.reference:
    """Return an inline reference to one store anchor."""
    ref = nodes.reference("", "", internal=True)
    if docname:
        ref["refuri"] = f"../{docname}/#{store_target_id(store.store_id)}"
    else:
        ref["refuri"] = f"#{store_target_id(store.store_id)}"
    ref += nodes.literal("", store.store_id)
    return ref


def _store_link(store: StoreDescriptor, *, docname: str = "") -> nodes.paragraph:
    """Return a paragraph linking to one store anchor."""
    paragraph = nodes.paragraph()
    paragraph += _store_reference(store, docname=docname)
    return paragraph


def _store_signature_link(store: StoreDescriptor) -> nodes.inline:
    """Return a text-element wrapper for a linked store signature."""
    inline = nodes.inline()
    inline += _store_reference(store)
    return inline


def _adapter_literal(store: StoreDescriptor) -> nodes.paragraph:
    """Return a paragraph containing adapter ids or a dash."""
    adapters = store_adapter_ids(store)
    return literal_paragraph(adapters) if adapters else text_paragraph("-")


def _literal_chip_list(values: Sequence[str]) -> nodes.paragraph:
    """Return a wrapping literal-chip paragraph."""
    paragraph = nodes.paragraph(classes=[StorageCSS.CHIP_LIST])
    if not values:
        paragraph += nodes.inline("", "-", classes=[StorageCSS.EMPTY_VALUE])
        return paragraph
    for value in values:
        paragraph += nodes.literal("", value)
    return paragraph


def _store_chip_list(stores: Sequence[StoreDescriptor]) -> nodes.paragraph:
    """Return a wrapping paragraph of linked store chips."""
    paragraph = nodes.paragraph(classes=[StorageCSS.CHIP_LIST])
    if not stores:
        paragraph += nodes.inline("", "-", classes=[StorageCSS.EMPTY_VALUE])
        return paragraph
    for store in stores:
        paragraph += _store_reference(store)
    return paragraph


def _key_value_section(rows: Sequence[ApiFactRow]) -> nodes.Element:
    """Return storage key/value facts using the shared API facts section."""
    return build_api_facts_section(
        rows,
        classes=(StorageCSS.BODY_SECTION, StorageCSS.KEY_VALUE),
    )


def _card_shell(entry: nodes.Element, *, classes: Sequence[str]) -> nodes.container:
    """Wrap a shared API card entry in a card shell container."""
    card = nodes.container(classes=[API.CARD_SHELL, *classes])
    card += entry
    return card


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
        _key_value_section(facts),
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


def _store_index_card(store: StoreDescriptor) -> nodes.container:
    """Build one compact summary card for a backend store."""
    adapter_values = tuple(dict.fromkeys(spec.adapter_id for spec in store.discovery))
    facts = [
        ApiFactRow("Role", literal_paragraph(store.role.value)),
        ApiFactRow("Format", literal_paragraph(store.format.value)),
        ApiFactRow("Coverage", literal_paragraph(store.coverage_level.value)),
        ApiFactRow("Adapter", _literal_chip_list(adapter_values)),
    ]
    entry = build_api_card_entry(
        profile_class=API.profile("storage-store-index"),
        signature_children=(_store_signature_link(store),),
        content_children=(_key_value_section(facts),),
        badge_group=build_store_badge_group(store.coverage_level.value),
        entry_classes=(StorageCSS.STORE_ENTRY,),
        signature_classes=(StorageCSS.STORE_SIGNATURE,),
    )
    return _card_shell(entry, classes=(StorageCSS.STORE_INDEX_CARD,))


def _agent_store_index(stores: Sequence[StoreDescriptor]) -> nodes.container:
    """Build the compact store index for one backend."""
    index = nodes.container(classes=[StorageCSS.STORE_INDEX])
    for store in stores:
        index += _store_index_card(store)
    return index


def _agent_stores(catalog: StoreCatalog, agent: str) -> tuple[StoreDescriptor, ...]:
    """Return catalog stores for an agent, validating the name."""
    known_agents = {store.agent for store in catalog.stores}
    if agent not in known_agents:
        msg = f"unknown storage agent: {agent}"
        raise ValueError(msg)
    return tuple(store for store in catalog.stores if store.agent == agent)


def _stores_matching(
    stores: Sequence[StoreDescriptor],
    predicate: t.Callable[[StoreDescriptor], bool],
) -> tuple[StoreDescriptor, ...]:
    """Return stores accepted by *predicate*."""
    return tuple(store for store in stores if predicate(store))


def _coverage_groups(
    stores: Sequence[StoreDescriptor],
) -> tuple[tuple[str, tuple[StoreDescriptor, ...]], ...]:
    """Return support categories for one agent's stores."""
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
    grouped_stores = (
        default_stores,
        inspectable_stores,
        catalog_stores,
        memory_stores,
        planning_stores,
        instruction_stores,
        index_stores,
        app_state_stores,
        raw_stores,
    )
    return tuple(zip(_SUPPORT_GROUP_LABELS, grouped_stores, strict=True))


def _support_agent_card(agent: str, stores: Sequence[StoreDescriptor]) -> nodes.container:
    """Build one support-matrix card for an agent."""
    rows = [
        ApiFactRow(label, _store_chip_list(group_stores))
        for label, group_stores in _coverage_groups(stores)
    ]
    entry = build_api_card_entry(
        profile_class=API.profile("storage-support-agent"),
        signature_children=(nodes.Text(_AGENT_LABELS.get(agent, agent.title())),),
        content_children=(_key_value_section(rows),),
        entry_classes=(StorageCSS.STORE_ENTRY,),
    )
    return _card_shell(entry, classes=(StorageCSS.SUPPORT_AGENT_CARD,))


def _coverage_grid(catalog: StoreCatalog) -> nodes.container:
    """Build support cards grouped by agent."""
    matrix = nodes.container(classes=[StorageCSS.SUPPORT_MATRIX])
    for agent in sorted({store.agent for store in catalog.stores}):
        stores = tuple(store for store in catalog.stores if store.agent == agent)
        matrix += _support_agent_card(agent, stores)
    return matrix


def _summary_card(title: str, counts: dict[str, int]) -> nodes.container:
    """Build one catalog-summary count card."""
    rows = [ApiFactRow(key, literal_paragraph(str(value))) for key, value in sorted(counts.items())]
    entry = build_api_card_entry(
        profile_class=API.profile("storage-catalog-summary"),
        signature_children=(nodes.Text(title),),
        content_children=(_key_value_section(rows),),
        entry_classes=(StorageCSS.STORE_ENTRY,),
    )
    return _card_shell(entry, classes=(StorageCSS.CATALOG_SUMMARY_CARD,))


def _catalog_summary(tables: Sequence[tuple[str, dict[str, int]]]) -> nodes.container:
    """Build the catalog summary card grid."""
    summary = nodes.container(classes=[StorageCSS.CATALOG_SUMMARY])
    for title, counts in tables:
        summary += _summary_card(title, counts)
    return summary


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
                _agent_store_index(stores),
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
        return [_catalog_summary(tables)]


class StorageCoverageGridDirective(SphinxDirective):
    """Render the backend support grid from the storage catalog."""

    required_arguments = 0
    optional_arguments = 0
    has_content = False

    def run(self) -> list[nodes.Node]:
        """Render the generated backend coverage grid."""
        catalog = _load_catalog(self.config)
        return [_coverage_grid(catalog)]
