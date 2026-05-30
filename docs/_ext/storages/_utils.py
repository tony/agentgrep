"""Small rendering helpers for storage documentation nodes."""

from __future__ import annotations

import re
import typing as t

from docutils import nodes
from sphinx_ux_autodoc_layout import parse_generated_markup

if t.TYPE_CHECKING:
    from sphinx.util.docutils import SphinxDirective

    from agentgrep.stores import StoreDescriptor


def slugify(value: str) -> str:
    """Return a stable HTML-id slug for a catalog value."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "item"


def store_target_id(store_id: str) -> str:
    """Return the canonical section id for one store."""
    return f"storage-store-{slugify(store_id)}"


def display_value(value: object) -> str:
    """Return a title-ish display string for enum/string values."""
    raw = getattr(value, "value", value)
    return str(raw).replace("_", " ").title()


def coverage_label(value: str) -> str:
    """Return compact coverage text for badges and tables."""
    return {
        "default_search": "default",
        "catalog_only": "catalog",
    }.get(value, value.replace("_", " "))


def literal_paragraph(text: str) -> nodes.paragraph:
    """Return a paragraph containing one literal node."""
    paragraph = nodes.paragraph()
    paragraph += nodes.literal("", text)
    return paragraph


def text_paragraph(text: str) -> nodes.paragraph:
    """Return a plain paragraph, using a dash for missing text."""
    return nodes.paragraph("", text if text else "-")


def markup_body(directive: SphinxDirective, text: str) -> list[nodes.Node]:
    """Parse generated store markup into body nodes (MyST/rST aware).

    Store ``schema_notes`` and ``search_notes`` are authored with
    single-backtick Markdown. Reparsing them through the page's active
    parser renders that markup as inline code instead of literal
    backticks. Empty text keeps the dash placeholder used elsewhere on
    the card.
    """
    if not text:
        empty: list[nodes.Node] = [text_paragraph("-")]
        return empty
    return parse_generated_markup(directive, text)


def store_adapter_ids(store: StoreDescriptor) -> str:
    """Return comma-separated adapter ids declared by a descriptor."""
    return ", ".join(dict.fromkeys(spec.adapter_id for spec in store.discovery))


def store_data_versions(store: StoreDescriptor) -> str:
    """Return comma-separated data-shape versions declared by a descriptor."""
    return ", ".join(
        dict.fromkeys(spec.data_version for spec in store.discovery if spec.data_version)
    )


def store_strategy_values(store: StoreDescriptor) -> str:
    """Return comma-separated version-detection strategy values."""
    return ", ".join(strategy.value for strategy in store.version_strategies)


def make_table(
    headers: t.Sequence[str],
    rows: t.Sequence[t.Sequence[str | nodes.Node]],
    *,
    classes: t.Sequence[str] = (),
) -> nodes.table:
    """Build a docutils table from string/node cells."""
    table = nodes.table(classes=list(classes))
    tgroup = nodes.tgroup(cols=len(headers))
    table += tgroup
    for _header in headers:
        tgroup += nodes.colspec(colwidth=1)

    thead = nodes.thead()
    tgroup += thead
    header_row = nodes.row()
    thead += header_row
    for header in headers:
        entry = nodes.entry()
        entry += nodes.paragraph("", header)
        header_row += entry

    tbody = nodes.tbody()
    tgroup += tbody
    for row in rows:
        table_row = nodes.row()
        tbody += table_row
        for cell in row:
            entry = nodes.entry()
            if isinstance(cell, nodes.Node):
                entry += cell
            else:
                entry += nodes.paragraph("", cell)
            table_row += entry
    return table


def comma_literal_list(values: t.Sequence[str]) -> nodes.paragraph:
    """Return comma-separated literal values in one paragraph."""
    paragraph = nodes.paragraph()
    if not values:
        paragraph += nodes.Text("-")
        return paragraph
    for index, value in enumerate(values):
        if index:
            paragraph += nodes.Text(", ")
        paragraph += nodes.literal("", value)
    return paragraph
