"""TUI surface for unregistered-field-predicate diagnostics (agentgrep#153).

``tests/test_query_gate.py`` proves the detection itself; this module
proves the HUD and grep-log layouts present the same non-fatal warning on
submit only — never on every keystroke through the live depth-offer
preview, which re-parses the search box on each edit (ADR 0011: a
notification is a "significant event" primitive, not something to fire on
every character typed).
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest

from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery
from agentgrep.ui.app import build_streaming_ui_app

pytestmark = pytest.mark.tui


def _idle_query() -> SearchQuery:
    """Build the launch plan of a cold session: prompt scope, no terms."""
    return SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=(),
        limit=None,
    )


async def test_live_typing_never_warns_on_the_hud_layout(tmp_path: pathlib.Path) -> None:
    """The live depth-offer preview must never fire a notification mid-edit.

    Regression guard for the bug this fix closes: an earlier design wired
    the warning straight into the query-build helper the live preview also
    calls on every keystroke, producing a toast per character typed after
    a colon.
    """
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _idle_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        layout = app.screen
        await pilot.pause()
        app._notifications.clear()

        layout._search_input.load_query("bogusfield:xyz")
        await pilot.pause()
        layout._search_input.load_query("bogusfield:x")
        await pilot.pause()

        assert list(app._notifications) == []


async def test_submitting_an_unregistered_field_predicate_warns_on_the_hud_layout(
    tmp_path: pathlib.Path,
) -> None:
    """Enter, with a typo'd field in the box, presents exactly one warning."""
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _idle_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        layout = app.screen
        await pilot.pause()
        layout._search_input.load_query("bogusfield:xyz")
        layout._search_input.focus()
        app._notifications.clear()

        await pilot.press("enter")
        await pilot.pause()

        titles = [notification.title for notification in app._notifications]
        assert titles == ["Unrecognized field"]
        (notification,) = app._notifications
        assert "bogusfield" in str(notification.message)


async def test_submitting_a_registered_field_predicate_does_not_warn(
    tmp_path: pathlib.Path,
) -> None:
    """A real field predicate needs no diagnostic on submit either."""
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _idle_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        layout = app.screen
        await pilot.pause()
        layout._search_input.load_query("kind:prompt")
        layout._search_input.focus()
        app._notifications.clear()

        await pilot.press("enter")
        await pilot.pause()

        assert list(app._notifications) == []
