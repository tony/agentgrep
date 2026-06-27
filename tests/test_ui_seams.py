"""The engine seam lets the app shell call narrow Protocols, not engine guts.

These structural tests prove a fake double satisfies each ``Protocol`` (so
widget/app tests can fake the engine) and that the concrete adapter is itself a
``SearchInvoker`` — without running a real search.
"""

from __future__ import annotations

import collections.abc as cabc
import pathlib

from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery
from agentgrep.ui._seams import EngineSearchInvoker, PreviewProvider, SearchInvoker


def _make_query() -> SearchQuery:
    """Build a minimal valid prompts-scope query."""
    return SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=(),
        limit=None,
    )


class _FakeInvoker:
    """A test double that records calls and emits a sentinel event."""

    def __init__(self) -> None:
        self.calls: list[SearchQuery] = []

    def run(
        self,
        query: SearchQuery,
        *,
        control: SearchControl,
        emit: cabc.Callable[[object], None],
    ) -> None:
        """Record ``query`` and emit a single ``"done"`` event."""
        self.calls.append(query)
        emit("done")


def test_fake_satisfies_search_invoker_protocol() -> None:
    """A structural fake is accepted as a SearchInvoker and forwards events."""
    invoker: SearchInvoker = _FakeInvoker()
    seen: list[object] = []
    invoker.run(_make_query(), control=SearchControl(), emit=seen.append)
    assert seen == ["done"]


def test_engine_invoker_satisfies_protocol() -> None:
    """EngineSearchInvoker is structurally a SearchInvoker (not run here)."""
    invoker: SearchInvoker = EngineSearchInvoker(pathlib.Path("/nonexistent"))
    assert callable(invoker.run)


class _FakePreview:
    """A test double that echoes the item into a preview body."""

    def fetch(self, item: object) -> str:
        """Return a deterministic preview body for ``item``."""
        return f"preview:{item}"


def test_fake_satisfies_preview_provider_protocol() -> None:
    """A structural fake is accepted as a PreviewProvider."""
    provider: PreviewProvider = _FakePreview()
    assert provider.fetch("x") == "preview:x"
