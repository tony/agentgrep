"""pi-lite semantic theme and design tokens for the Textual explorer.

Two themes — :func:`agentgrep_dark` and :func:`agentgrep_light` — map a
pi-inspired "lite" palette onto Textual's seed colors so every built-in widget
re-skins through :class:`textual.design.ColorSystem` for free (one accent, calm
muted secondary text, flat surfaces). agentgrep-specific semantics that have no
seed equivalent live as ``$ag-*`` custom variables: per-agent hues, the
``prompt`` / ``history`` kinds, the muted/dim/faint text trio, the welcome
wordmark shine, state-tint backgrounds, and search/filter match highlights.

On-tint foregrounds (``$ag-on-*``) and match foregrounds are *computed* from
each background via :meth:`textual.color.Color.get_contrast_text`, so a tinted
badge or selected row stays readable in both themes by construction rather than
by a hand-picked guess.

The ``$ag-*`` variables hold concrete ``#rrggbb`` literals (never ``$token``
references) because the results list and detail header paint Rich renderables
outside the stylesheet's reach: :func:`resolve` reads
:attr:`textual.app.App.theme_variables` to feed those Rich spans, while the
stylesheet consumes the same tokens directly.

This module imports Textual at import time and is reached only through
:func:`agentgrep.ui.app.build_streaming_ui_app` (and the theme tests), never the
eager ``import agentgrep`` path.
"""

from __future__ import annotations

import collections.abc as cabc

from textual.color import Color
from textual.theme import Theme

__all__ = [
    "AGENT_TOKEN_BY_NAME",
    "DARK_THEME_NAME",
    "KIND_TOKEN_BY_NAME",
    "LIGHT_THEME_NAME",
    "ag_variable_defaults",
    "agentgrep_dark",
    "agentgrep_light",
    "resolve",
]

DARK_THEME_NAME = "agentgrep-dark"
LIGHT_THEME_NAME = "agentgrep-light"

# --- pi palette tables: token base name -> (dark hex, light hex) -----------
#
# Hues are chosen to read on both the dark page (#18181e) and the light page
# (#f8f8f8). The four agents that were already styled keep their hue family
# (codex cyan, claude magenta, cursor gold); the seven that shipped unstyled
# gain distinct hues. ``pi`` borrows the accent teal — it is the inspiration.

_AGENT_HUES: dict[str, tuple[str, str]] = {
    "codex": ("#00d7ff", "#0087af"),
    "claude": ("#d183e8", "#9c27b0"),
    "cursor-cli": ("#e5c07b", "#b8860b"),
    "cursor-ide": ("#ffd75f", "#8a6d3b"),
    "gemini": ("#8ab4f8", "#1a73e8"),
    "antigravity-cli": ("#5fd7af", "#2a9d8f"),
    "antigravity-ide": ("#56b6c2", "#0e7490"),
    "grok": ("#abb2bf", "#5c6370"),
    "pi": ("#8abeb7", "#5a8080"),
    "opencode": ("#98c379", "#4d8a35"),
    "vscode": ("#61afef", "#0066b8"),
}

_KIND_HUES: dict[str, tuple[str, str]] = {
    "prompt": ("#b5bd68", "#588458"),
    "history": ("#5f87ff", "#547da7"),
}

# Muted/dim/faint text trio + the model hue (pi's customMessageLabel purple).
# This trio is the app-wide contrast dial: muted=paths, dim=timestamps, faint=
# at-rest input/pane rules. The dark column trends brighter and the light column
# darker than a bare gray so each tier stays legible against its page.
_TEXT_HUES: dict[str, tuple[str, str]] = {
    "ag-muted": ("#909090", "#5c5c5c"),
    "ag-dim": ("#767676", "#666666"),
    "ag-faint": ("#5a5a5a", "#a0a0a0"),
    "ag-model": ("#9575cd", "#7e57c2"),
}

# Static warm ramp for the welcome wordmark. The dark and light columns are
# calibrated independently so every step clears WCAG AA against its page;
# animation would add idle repaints without improving search affordance.
_BRAND_SHINE_HUES: tuple[tuple[str, str], ...] = (
    ("#ff7a1a", "#9a3e00"),
    ("#ff922b", "#994900"),
    ("#ffaa3b", "#925300"),
    ("#ffc04d", "#875a00"),
    ("#ffd166", "#765f00"),
)

# Subtle state-tint backgrounds (pi's message/tool background family).
_STATE_BG_HUES: dict[str, tuple[str, str]] = {
    "user": ("#343541", "#e8e8e8"),
    "pending": ("#282832", "#e8e8f0"),
    "success": ("#283228", "#e8f0e8"),
    "error": ("#3c2828", "#f0e8e8"),
    "selected": ("#3a3a4a", "#d0d0e0"),
}

# Match highlights. Search terms recur throughout a body, so they get a calm
# colored *foreground* (gold on dark, dark-gold on light — readable on both,
# unlike a flat "yellow" that vanishes on a light page). Filter terms are a
# deliberate refinement, so they get a prominent *background* fill (the accent)
# with a contrast-computed foreground. The two never read as the same thing.
_MATCH_SEARCH_FG: tuple[str, str] = ("#ffd75f", "#b8860b")
_MATCH_FILTER_BG: tuple[str, str] = ("#8abeb7", "#5a8080")

#: ``record.agent`` -> ``$ag-*`` variable name (without the ``$``). Keyed by
#: every member of :data:`agentgrep.records.AGENT_CHOICES`.
AGENT_TOKEN_BY_NAME: dict[str, str] = {name: f"ag-agent-{name}" for name in _AGENT_HUES}
#: ``record.kind`` -> ``$ag-*`` variable name (without the ``$``).
KIND_TOKEN_BY_NAME: dict[str, str] = {name: f"ag-kind-{name}" for name in _KIND_HUES}


def _on(background_hex: str) -> str:
    """Return a readable foreground hex for a tinted ``background_hex``.

    Parameters
    ----------
    background_hex : str
        A ``#rrggbb`` background color.

    Returns
    -------
    str
        ``#ffffff`` or ``#000000`` — whichever Textual's luminance test picks
        for maximum contrast against ``background_hex``.
    """
    return Color.parse(background_hex).get_contrast_text(1.0).hex6


def _ag_variables(mode: int) -> dict[str, str]:
    """Build the ``$ag-*`` custom-variable map for a palette ``mode``.

    Parameters
    ----------
    mode : int
        ``0`` selects the dark hex of each ``(dark, light)`` pair, ``1`` the
        light hex.

    Returns
    -------
    dict[str, str]
        Variable name (without ``$``) to concrete ``#rrggbb`` literal.
    """
    variables: dict[str, str] = {}
    for name, hexes in _AGENT_HUES.items():
        variables[f"ag-agent-{name}"] = hexes[mode]
    for name, hexes in _KIND_HUES.items():
        variables[f"ag-kind-{name}"] = hexes[mode]
    for name, hexes in _TEXT_HUES.items():
        variables[name] = hexes[mode]
    for step, hexes in enumerate(_BRAND_SHINE_HUES, start=1):
        variables[f"ag-brand-shine-{step}"] = hexes[mode]
    for name, hexes in _STATE_BG_HUES.items():
        background = hexes[mode]
        variables[f"ag-state-{name}-bg"] = background
        variables[f"ag-on-{name}"] = _on(background)
    variables["ag-match-search"] = _MATCH_SEARCH_FG[mode]
    filter_background = _MATCH_FILTER_BG[mode]
    variables["ag-match-filter-bg"] = filter_background
    variables["ag-match-filter-fg"] = _on(filter_background)
    return variables


# Built-in Textual variables we override for a flat, pi-accented feel: the
# selection block cursor and footer key both adopt the accent (cursor kept flat,
# no reverse/bold, the way pi renders selected rows). The scrollbar palette is
# kept pi-lite (a quiet faint thumb on a surface-blended track, accent on
# hover/drag) for completeness — Textual generates these for every theme — but
# the lite layout hides scrollbars entirely (``scrollbar-size: 0`` in styles.tcss,
# vim/pi-style), so this palette only renders if a scrollbar is re-enabled.
#: ``surface`` seed per mode (track color), mirrors the Theme ``surface`` values.
_SURFACE_HEX: tuple[str, str] = ("#1e1e24", "#ffffff")


def _builtin_overrides(mode: int) -> dict[str, str]:
    """Return pi-flat overrides for Textual's own widget tokens."""
    accent = _AGENT_HUES["pi"][mode]
    faint = _TEXT_HUES["ag-faint"][mode]
    surface = _SURFACE_HEX[mode]
    return {
        "block-cursor-background": accent,
        "block-cursor-foreground": _on(accent),
        "block-cursor-text-style": "none",
        "footer-key-foreground": accent,
        "input-cursor-background": accent,
        "input-cursor-foreground": _on(accent),
        "scrollbar": faint,
        "scrollbar-hover": accent,
        "scrollbar-active": accent,
        "scrollbar-background": surface,
        "scrollbar-background-hover": surface,
        "scrollbar-background-active": surface,
        "scrollbar-corner-color": surface,
    }


def agentgrep_dark() -> Theme:
    """Return the pi-lite dark theme.

    Returns
    -------
    textual.theme.Theme
        Registered under :data:`DARK_THEME_NAME`.
    """
    return Theme(
        name=DARK_THEME_NAME,
        primary="#8abeb7",
        secondary="#5f87ff",
        accent="#8abeb7",
        warning="#ffff00",
        error="#cc6666",
        success="#b5bd68",
        foreground="#d4d4d4",
        background="#18181e",
        surface="#1e1e24",
        panel="#26262e",
        dark=True,
        variables={**_ag_variables(0), **_builtin_overrides(0)},
    )


def agentgrep_light() -> Theme:
    """Return the pi-lite light theme.

    Returns
    -------
    textual.theme.Theme
        Registered under :data:`LIGHT_THEME_NAME`.
    """
    return Theme(
        name=LIGHT_THEME_NAME,
        primary="#5a8080",
        secondary="#547da7",
        accent="#5a8080",
        warning="#9a7326",
        error="#aa5555",
        success="#588458",
        foreground="#1f2328",
        background="#f8f8f8",
        surface="#ffffff",
        panel="#eeeeee",
        dark=False,
        variables={**_ag_variables(1), **_builtin_overrides(1)},
    )


def ag_variable_defaults() -> dict[str, str]:
    """Return the ``$ag-*`` tokens used as app-wide variable defaults.

    Supplied via :meth:`textual.app.App.get_theme_variable_defaults` so the
    stylesheet's ``$ag-*`` references resolve under *any* active theme: the
    pi-lite themes override these with their own light/dark values, while an
    activated built-in theme falls back to this dark set rather than raising an
    unresolved-variable error.

    Returns
    -------
    dict[str, str]
        The dark ``$ag-*`` variable map (without the ``$``).
    """
    return _ag_variables(0)


def resolve(theme_variables: cabc.Mapping[str, str], name: str | None) -> str:
    """Resolve an ``$ag-*`` variable ``name`` to a concrete hex for Rich.

    Rich's :meth:`rich.text.Text.append` needs a literal color, not a Textual
    ``$token``. This reads the active theme's variable map (from
    :attr:`textual.app.App.theme_variables`).

    Parameters
    ----------
    theme_variables : collections.abc.Mapping[str, str]
        The app's resolved theme-variable map.
    name : str | None
        A variable name without the ``$`` (e.g. ``"ag-agent-codex"``), or
        ``None`` for "no styling".

    Returns
    -------
    str
        The concrete ``#rrggbb`` hex, or ``""`` when ``name`` is falsy or the
        variable is absent — an empty Rich style inherits the default color.
    """
    if not name:
        return ""
    return theme_variables.get(name, "")
