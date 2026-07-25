"""User-configurable keymap overrides for the Textual explorer.

The explorer ships one binding set that binds the arrow, vim (``hjkl``), and
emacs (``ctrl+n`` / ``ctrl+p``) motions all at once, so navigation works out of
the box without choosing a mode. To customize, drop a ``keymaps.toml`` in
``$XDG_CONFIG_HOME/agentgrep`` with a ``[bindings]`` table mapping a remappable
:class:`~textual.binding.Binding` ``id`` to a comma-joined key string; it is
applied at startup through Textual's native ``App.set_keymap``. This module only
parses ``str``->``str`` data and never imports Textual, so it stays
unit-testable offline (mirroring how ``commands.py`` and ``completion.py`` stay
frontend-neutral).

Safety: the read is bounded (64 KiB cap) and degrades silently -- a missing,
oversized, malformed, or wrong-typed file yields an empty override, which leaves
every binding's authored keys in place. Only ids agentgrep declares in
:data:`REMAPPABLE_BINDING_IDS` are honored; an id-less app-invariant chord
(quit, tab, escape, ``ctrl+r``, ``ctrl+hjkl`` pane focus) cannot be shadowed,
and a typo cannot invent a new action.

The whole surface is filesystem-touching, so it is exercised by unit tests with
fixtures rather than doctests (per the project doctest policy).
"""

from __future__ import annotations

import collections.abc as cabc
import logging
import os
import pathlib
import tomllib

__all__ = [
    "REMAPPABLE_BINDING_IDS",
    "load_keymap_override",
    "user_keymap_path",
]

logger = logging.getLogger(__name__)

#: Bound on the keymap file, matching the preferences payload cap.
_MAX_KEYMAP_BYTES = 64 * 1024

#: Binding ids a keymap file may rebind. Anything outside this set is dropped on
#: load so a user file can neither typo a new action into existence nor shadow
#: the id-less app-invariant chords.
REMAPPABLE_BINDING_IDS: frozenset[str] = frozenset(
    {
        "results.cursor_up",
        "results.cursor_down",
        "results.focus_detail",
        "results.cursor_top",
        "results.cursor_bottom",
        "results.half_page_down",
        "results.half_page_up",
        "detail.scroll_up",
        "detail.scroll_down",
        "detail.focus_results",
        "detail.scroll_home",
        "detail.scroll_end",
        "detail.scroll_half_down",
        "detail.scroll_half_up",
        "detail.open_find",
        "detail.toggle_raw",
        "detail.copy_source",
        "detail.copy_rendered",
    },
)


def user_keymap_path(
    *,
    environ: cabc.Mapping[str, str] | None = None,
    home: pathlib.Path | None = None,
) -> pathlib.Path:
    """Return the path of the user keymap file under the XDG config home.

    Parameters
    ----------
    environ : collections.abc.Mapping[str, str] | None
        Environment mapping; defaults to :data:`os.environ`.
    home : pathlib.Path | None
        Home directory fallback; defaults to :meth:`pathlib.Path.home`.

    Returns
    -------
    pathlib.Path
        ``<config home>/agentgrep/keymaps.toml``.
    """
    environment = os.environ if environ is None else environ
    root = environment.get("XDG_CONFIG_HOME")
    config_home = pathlib.Path(root) if root else (home or pathlib.Path.home()) / ".config"
    return config_home / "agentgrep" / "keymaps.toml"


def load_keymap_override(
    *,
    environ: cabc.Mapping[str, str] | None = None,
    home: pathlib.Path | None = None,
) -> dict[str, str]:
    """Load the user's ``{binding id: keys}`` override, or ``{}`` when absent.

    Reads the ``[bindings]`` table of :func:`user_keymap_path`, keeping only
    allowlisted ids with a non-empty string value. Any read/parse problem
    degrades to ``{}`` (leaving the authored keys in place). Called once
    off-pump at App construction so the result can be handed straight to
    ``App.set_keymap``.

    Returns
    -------
    dict[str, str]
        The resolved override map (possibly empty).
    """
    path = user_keymap_path(environ=environ, home=home)
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_KEYMAP_BYTES + 1)
    except FileNotFoundError:
        return {}
    except OSError:
        logger.warning("keymap file unreadable", extra={"agentgrep_keymap_file": path.name})
        return {}
    if len(raw) > _MAX_KEYMAP_BYTES:
        logger.warning("keymap file too large", extra={"agentgrep_keymap_file": path.name})
        return {}
    try:
        payload = tomllib.loads(raw.decode("utf-8"))
    except UnicodeError, tomllib.TOMLDecodeError:
        logger.warning("keymap file malformed", extra={"agentgrep_keymap_file": path.name})
        return {}
    bindings = payload.get("bindings")
    if not isinstance(bindings, dict):
        return {}
    resolved: dict[str, str] = {}
    for binding_id, keys in bindings.items():
        if binding_id in REMAPPABLE_BINDING_IDS and isinstance(keys, str) and keys.strip():
            resolved[binding_id] = keys.strip()
    return resolved
