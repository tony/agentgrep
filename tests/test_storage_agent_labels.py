"""Every catalogue agent must have a storage-docs display label.

An unlabelled agent falls back to :meth:`str.title`, which writes ``Opencode``
into published HTML without failing anything.
"""

from __future__ import annotations

from agentgrep.store_catalog import CATALOG
from docs._ext.storages._directives import _AGENT_LABELS


def test_every_catalog_agent_has_a_display_label() -> None:
    """Each agent in the catalogue resolves to an explicit label."""
    agents = {store.agent for store in CATALOG.stores}
    assert agents <= set(_AGENT_LABELS), (
        f"storage docs would title-case these agent ids: {sorted(agents - set(_AGENT_LABELS))}"
    )


def test_multiword_agent_ids_are_not_title_cased() -> None:
    """Hyphenated ids get a real product spelling.

    Single-word products legitimately match their own title case, so equality
    alone proves nothing; a hyphen is where :meth:`str.title` goes wrong.
    """
    wrong = {
        agent: label
        for agent, label in _AGENT_LABELS.items()
        if "-" in agent and label == agent.title()
    }
    assert not wrong, f"these hyphenated ids kept the title-case fallback: {sorted(wrong)}"
