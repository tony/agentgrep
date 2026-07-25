"""User keymap override: all-families default + one editable config file.

Covers the keymap surface: the shipped bindings bind the arrow, vim (``hjkl``),
and emacs (``ctrl+n`` / ``ctrl+p``) motions at once (no mode to pick), and an
optional ``$XDG_CONFIG_HOME/agentgrep/keymaps.toml`` rebinds any allowlisted
binding id, applied at startup through Textual's native ``set_keymap``.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest
from textual.binding import Binding, BindingType

from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery
from agentgrep.ui import keymaps
from agentgrep.ui.app import build_streaming_ui_app
from agentgrep.ui.widgets.detail import DetailScroll
from agentgrep.ui.widgets.results import SearchResultsList


def _empty_query() -> SearchQuery:
    """Build an idle search query (no terms)."""
    return SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=(),
        limit=None,
    )


def _keys_for(bindings: list[BindingType], binding_id: str) -> set[str]:
    """Return the keys the authored ``bindings`` bind to ``binding_id``."""
    keys: set[str] = set()
    for binding in bindings:
        if isinstance(binding, Binding) and binding.id == binding_id:
            keys.update(binding.key.split(","))
    return keys


# --- all-families default (no mode to select) ------------------------------


def test_default_bindings_bind_arrow_vim_and_emacs_motions_at_once() -> None:
    """cursor/scroll up-down bind arrows + hjkl + emacs ctrl+n/p simultaneously."""
    assert _keys_for(SearchResultsList.BINDINGS, "results.cursor_up") == {"up", "k", "ctrl+p"}
    assert _keys_for(SearchResultsList.BINDINGS, "results.cursor_down") == {"down", "j", "ctrl+n"}
    assert _keys_for(DetailScroll.BINDINGS, "detail.scroll_up") == {"up", "k", "ctrl+p"}
    assert _keys_for(DetailScroll.BINDINGS, "detail.scroll_down") == {"down", "j", "ctrl+n"}


# --- override file loading -------------------------------------------------


def test_override_file_rebinds_only_allowlisted_ids(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keymaps.toml rebinds an allowlisted id and drops everything else.

    The id-less invariant chords (``smart_quit`` etc.) are unshadowable and a
    typo cannot invent a new action, so only the allowlisted entry survives.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = keymaps.user_keymap_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[bindings]\n"
        '"results.cursor_up" = "e"\n'  # allowlisted -> honored
        '"smart_quit" = "j"\n'  # invariant intent -> dropped
        '"totally.made.up" = "q"\n',  # unknown id -> dropped
        encoding="utf-8",
    )
    override = keymaps.load_keymap_override()
    assert override == {"results.cursor_up": "e"}
    assert not ({"smart_quit", "totally.made.up"} & keymaps.REMAPPABLE_BINDING_IDS)


def test_missing_and_malformed_override_degrade_to_empty(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No file, or a malformed one, yields ``{}`` so authored keys stay in place."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert keymaps.load_keymap_override() == {}  # no file present
    path = keymaps.user_keymap_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is = not valid toml [[[", encoding="utf-8")
    assert keymaps.load_keymap_override() == {}


# --- live: startup applies the override ------------------------------------


@pytest.mark.tui
async def test_startup_applies_override_to_app_keymap(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The keymaps.toml override reaches the running app's keymap at mount."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = keymaps.user_keymap_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[bindings]\n"results.cursor_up" = "e"\n', encoding="utf-8")

    app = build_streaming_ui_app(tmp_path, _empty_query(), control=SearchControl())
    typed_app = t.cast("t.Any", app)
    async with typed_app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert typed_app._keymap.get("results.cursor_up") == "e"
