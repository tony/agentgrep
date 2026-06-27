"""Slash commands for the explorer's search box (``/clear``, ``/help``).

A tiny registry of ``/``-prefixed commands the user can run from the top search
input instead of a query — pi-style. The registry is the single source of truth
for both the ``/`` menu (which lists and filters it) and dispatch (which
resolves a typed token to a handler).

Kept Textual-free: handlers receive the running app and act through its existing
helpers, so the registry, alias resolution, parsing, and prefix filter are plain
functions unit-testable offline (mirroring how ``query/`` stays frontend-neutral).
The on-disk/runtime ``app`` is typed ``t.Any`` so a handler never couples this
module to the Textual app class.
"""

from __future__ import annotations

import dataclasses
import typing as t

if t.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = [
    "SLASH_COMMANDS",
    "SlashCommand",
    "command_matches",
    "parse_command",
    "resolve_command",
]


@dataclasses.dataclass(frozen=True, slots=True)
class SlashCommand:
    """One slash command: a canonical name, extra alias tokens, help, and a handler.

    ``name`` and ``aliases`` are bare tokens (no leading ``/``). ``run`` receives
    the running app and the raw argument remainder (everything after the command
    token); most commands ignore the args.
    """

    name: str
    aliases: tuple[str, ...]
    description: str
    run: cabc.Callable[[t.Any, str], None]


def _run_clear(app: t.Any, args: str) -> None:
    """Clear the search box and results, returning to the bare-canvas empty state.

    Mirrors the no-text branch of ``on_search_requested`` (the "select-all +
    delete + Enter" reset) but reachable as a named command, plus emptying the
    ``#search`` box and hiding the command menu. Not recorded to history —
    dispatch returns before ``_record_history`` runs. Signal the active control
    before resetting so menu selection cancels the same as a typed reset.
    """
    del args
    if app._search_input is not None:
        app._search_input.value = ""
        app._search_input.cursor_position = 0
    if app._enum_dropdown is not None:
        app._enum_dropdown.display = False
    app._command_matches = ()
    app.control.request_answer_now()
    app._reset_search_chrome()
    app._search_done = True
    app._set_empty_state(empty=True)
    app.query = app._build_search_query("")
    if app._search_input is not None:
        app._search_input.focus()


def _run_exit(app: t.Any, args: str) -> None:
    """Quit the explorer."""
    del args
    app.exit()


def _run_help(app: t.Any, args: str) -> None:
    """Show the available slash commands and their descriptions as a notification."""
    del args
    lines = [f"{_command_label(cmd)} — {cmd.description}" for cmd in SLASH_COMMANDS]
    app.notify("\n".join(lines), title="Slash commands", timeout=10)


def _command_label(cmd: SlashCommand) -> str:
    """Render ``/name (/alias1, /alias2)`` for menus and help (argparse-style)."""
    label = f"/{cmd.name}"
    if cmd.aliases:
        label += " (" + ", ".join(f"/{alias}" for alias in cmd.aliases) + ")"
    return label


SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("clear", ("new", "reset"), "Clear search and results", _run_clear),
    SlashCommand("exit", ("quit",), "Quit agentgrep", _run_exit),
    SlashCommand("help", (), "List slash commands", _run_help),
)
"""The ordered command registry — drives both the ``/`` menu and dispatch."""


_COMMAND_BY_TOKEN: dict[str, SlashCommand] = {
    token: cmd for cmd in SLASH_COMMANDS for token in (cmd.name, *cmd.aliases)
}
"""Flat token -> command lookup; one entry per name and per alias."""


def resolve_command(token: str) -> SlashCommand | None:
    """Resolve a command token (alias-aware) to its record, or ``None``.

    Tolerates a leading ``/`` and any case, so ``/Clear``, ``new``, and
    ``reset`` all resolve to the same record.
    """
    return _COMMAND_BY_TOKEN.get(token.lower().lstrip("/"))


def parse_command(text: str) -> tuple[str, str]:
    """Split a ``/command args`` line into ``(lowercased token, trimmed args)``.

    The leading ``/`` is dropped; the token is everything up to the first space,
    the args everything after. A bare ``/`` parses to ``("", "")``.
    """
    body = text.strip().removeprefix("/")
    token, _, args = body.partition(" ")
    return token.lower(), args.strip()


def command_matches(prefix: str) -> tuple[SlashCommand, ...]:
    """Return the commands whose name or any alias starts with ``prefix``.

    A bare/empty prefix lists every command; each command appears at most once
    even when several of its tokens share the prefix.
    """
    needle = prefix.lower().lstrip("/")
    return tuple(
        cmd
        for cmd in SLASH_COMMANDS
        if any(token.startswith(needle) for token in (cmd.name, *cmd.aliases))
    )
