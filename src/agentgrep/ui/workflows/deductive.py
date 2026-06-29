"""``DeductiveWorkflow`` — narrow a fixed haystack, widen by popping (ADR 0014).

The third interaction strategy, after ``search`` (every submit re-greps) and
``browse`` (load once, filter the whole set). Deductive search builds a *stack*
of refinements: the first non-empty submit runs one engine search that fixes the
haystack; each later submit pushes a stricter refinement and narrows the loaded
set *in-memory* (a composed ``AND`` over the records already loaded — confirmed
the cheap, engine-supported path); ``widen`` pops the top refinement and re-filters
with the weaker query; ``clear`` resets. The narrowing path is reported through
``WorkflowHost.update_breadcrumb`` and a pop re-seeds the prompt via
``set_input_text``.

The refinement stack is an immutable ``tuple`` of frozen frames held on the
workflow object — Textual-free, so the strategy is unit-testable against a fake
host. Narrowing routes through ``filter_loaded`` only, so a future "re-grep from
disk" escape hatch is a drop-in: compose every frame (including the base) and
call ``run_search`` instead — no data-model change.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import typing as t

from textual.binding import Binding

if t.TYPE_CHECKING:
    from agentgrep.ui.workflows._protocol import WorkflowHost

__all__ = ["DeductiveWorkflow", "RefinementFrame"]


@dataclasses.dataclass(frozen=True, slots=True)
class RefinementFrame:
    """One level of the narrowing stack: the literal text the user submitted."""

    text: str


class DeductiveWorkflow:
    """Route the primary input to a stacked, in-memory narrowing of a haystack."""

    name: t.ClassVar[str] = "deductive"
    summary: t.ClassVar[str] = "Narrow a fixed haystack; widen pops back out"
    #: Priority so the keys fire while the prompt is focused (like the app's F2/F3).
    BINDINGS: t.ClassVar[cabc.Sequence[object]] = (
        Binding("ctrl+up", 'workflow("widen")', "Widen", priority=True),
        Binding("ctrl+l", 'workflow("clear")', "Clear", priority=True),
    )

    def __init__(self) -> None:
        self._stack: tuple[RefinementFrame, ...] = ()

    def on_attach(self, host: WorkflowHost) -> None:
        """Seed the haystack from the launch query if it has terms, else go idle."""
        query = host.context.query
        if query.terms:
            self._stack = (RefinementFrame(" ".join(query.terms)),)
            host.run_search(query)
        else:
            self._stack = ()
            host.reset_view()
        host.update_breadcrumb(self._labels())

    def on_query(self, host: WorkflowHost, text: str) -> None:
        """First submit sets the haystack; later submits narrow it in-memory."""
        if not text:
            self._stack = ()
            host.request_cancel()
            host.reset_view()
            host.update_breadcrumb(())
            return
        host.record_history(text)
        if not self._stack:
            self._stack = (RefinementFrame(text),)
            host.run_search(host.build_query(text))
        else:
            self._stack = (*self._stack, RefinementFrame(text))
            host.request_cancel()
            host.filter_loaded(self._composed())
        host.update_breadcrumb(self._labels())

    def on_action(self, host: WorkflowHost, action_id: str) -> bool:
        """Handle ``widen`` (pop a level) and ``clear`` (reset) key actions."""
        if action_id == "widen":
            if len(self._stack) <= 1:
                return True
            self._stack = self._stack[:-1]
            host.filter_loaded(self._composed())
            host.set_input_text(self._stack[-1].text)
            host.update_breadcrumb(self._labels())
            return True
        if action_id == "clear":
            self._stack = ()
            host.request_cancel()
            host.reset_view()
            host.set_input_text("")
            host.update_breadcrumb(())
            return True
        return False

    def _composed(self) -> str:
        """Compose the in-memory ``AND`` filter over refinements past the base.

        The base frame is the engine query (already applied by ``run_search``);
        only the later refinements filter the loaded set. An empty result (widen
        back to the base alone) clears the filter, showing the full haystack.
        """
        return " AND ".join(f"({frame.text})" for frame in self._stack[1:])

    def _labels(self) -> tuple[str, ...]:
        """Return the breadcrumb labels for the current stack."""
        return tuple(frame.text for frame in self._stack)
