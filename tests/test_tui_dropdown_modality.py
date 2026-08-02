"""Focus-trap and dropdown-modality regression tests.

Two reported symptoms, one shared architectural gap: agentgrep's transient
overlay/highlight widgets never subscribed to Textual's ``Blur`` event, so
neither released control the way Textual's own ``Select``/``SelectOverlay``
does.

- ``DepthOffer`` (the idle canvas's "Deep search" / "Search all
  conversations" rows) defaulted to ``FOCUS_ON_CLICK = True``, so an
  incidental click anywhere in its block grabbed keyboard focus with no
  click-driven way to release it — a mouse user reads this as "the mouse
  gets stuck there."
- ``CompletionDropdown`` (the field-value completion popup) never
  implemented blur-driven dismissal, so it stayed open and visually
  floating after focus moved to any other widget — other parts of the
  screen stayed fully reachable while it was still shown.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest

from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery
from agentgrep.ui.app import build_streaming_ui_app
from agentgrep.ui.widgets.welcome import DepthOffer

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


async def test_clicking_the_depth_offer_block_does_not_trap_focus(
    tmp_path: pathlib.Path,
) -> None:
    """A click inside the block (not on an actionable row) never grabs focus.

    Regression guard for the reported "mouse gets to Deep search / Search
    all conversations and can't leave it": Textual's default
    ``FOCUS_ON_CLICK`` would focus ``DepthOffer`` on a click anywhere in its
    bounding box, and nothing in this app ever blurs it back — a mouse
    click is the only way in, and there was no click-driven way out.
    """
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _idle_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        layout = app.screen
        await pilot.pause()
        offer = layout.query_one("#empty-depth", DepthOffer)
        assert offer.FOCUS_ON_CLICK is False

        # Offset (0, 0) is the dim lead line, which carries no
        # DEPTH_OFFER_ACTION_META span — clicking it must not select a row
        # (which would tear down the idle canvas) or grab focus.
        await pilot.click(offer, offset=(0, 0))
        await pilot.pause()

        assert app.focused is not offer


async def test_depth_offer_still_reachable_and_selectable_by_mouse_and_keyboard(
    tmp_path: pathlib.Path,
) -> None:
    """Neither affordance the class docstring promises regresses.

    A click on an actual row still selects it (independent of focus), and
    Tab still reaches the panel (FOCUS_ON_CLICK does not gate the focus
    chain).
    """
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _idle_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        layout = app.screen
        offer = layout.query_one("#empty-depth", DepthOffer)
        await pilot.pause()

        await pilot.press("tab")
        assert app.focused is offer

        layout._search_input.load_query("needle")
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert layout._run_summary is not None


async def test_completion_dropdown_dismisses_when_input_loses_focus_elsewhere(
    tmp_path: pathlib.Path,
) -> None:
    """Focus moving to any other widget dismisses a still-open dropdown.

    Regression guard for "other areas shouldn't be reachable when the
    dropdown is open": before this fix the dropdown had no blur handling
    at all, so it stayed visually open and stale once focus moved away.
    """
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _idle_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        layout = app.screen
        await pilot.pause()
        layout._search_input.focus()
        layout._search_input.load_query("agent:")
        await pilot.pause()

        dropdown = layout._enum_dropdown
        assert dropdown.display is True

        layout._filter_input.focus()
        await pilot.pause()

        assert dropdown.display is False


async def test_completion_dropdown_survives_arrowing_into_it(
    tmp_path: pathlib.Path,
) -> None:
    """Pressing Down to navigate into the dropdown must not dismiss it.

    The blur-dismissal fix has to distinguish "focus left for elsewhere"
    from "focus moved onto the dropdown itself, deliberately" — this is
    the exemption that makes that distinction.
    """
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _idle_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        layout = app.screen
        await pilot.pause()
        layout._search_input.focus()
        layout._search_input.load_query("agent:")
        await pilot.pause()

        dropdown = layout._enum_dropdown
        assert dropdown.display is True

        await pilot.press("down")
        await pilot.pause()

        assert dropdown.display is True
        assert app.focused is dropdown


async def test_completion_dropdown_dismisses_when_it_loses_focus_itself(
    tmp_path: pathlib.Path,
) -> None:
    """The dropdown's own blur handler, distinct from the input's.

    Covers the case ``test_completion_dropdown_survives_arrowing_into_it``
    reaches but doesn't finish: focus is on the dropdown itself (the user
    arrowed in with Down), and then leaves it — a Tab, here. That case
    never touches ``SearchInput._on_blur`` at all, since the input wasn't
    the one holding focus; only ``CompletionDropdown._on_blur`` can close
    it.
    """
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _idle_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        layout = app.screen
        await pilot.pause()
        layout._search_input.focus()
        layout._search_input.load_query("agent:")
        await pilot.pause()

        dropdown = layout._enum_dropdown
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is dropdown

        await pilot.press("tab")
        await pilot.pause()

        assert app.focused is not dropdown
        assert dropdown.display is False
