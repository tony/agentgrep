"""Slash commands for the explorer's search box (``/clear``, ``/help``).

A tiny registry of ``/``-prefixed commands the user can run from the top search
input instead of a query — pi-style. The registry is the single source of truth
for both the ``/`` menu (which lists and filters it) and dispatch (which
resolves a typed token to a handler).

Kept Textual-free: handlers receive the active layout and act through its
existing helpers, so the registry, alias resolution, parsing, and prefix filter
are plain functions unit-testable offline (mirroring how ``query/`` stays
frontend-neutral). The runtime layout is typed ``t.Any`` so a handler never
couples this module to a Textual layout class.
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
    "command_menu_label",
    "parse_command",
    "resolve_command",
]


@dataclasses.dataclass(frozen=True, slots=True)
class SlashCommand:
    """One slash command: a canonical name, extra alias tokens, help, and a handler.

    ``name`` and ``aliases`` are bare tokens (no leading ``/``). ``run`` receives
    the active layout and the raw argument remainder (everything after the
    command token), then reports whether execution succeeded. ``argument_hint``
    is display-only; ``accepts_args`` independently controls dispatch.
    """

    name: str
    aliases: tuple[str, ...]
    description: str
    run: cabc.Callable[[t.Any, str], bool]
    argument_hint: str = ""
    accepts_args: bool = False


def _run_clear(app: t.Any, args: str) -> bool:
    """Cancel active work and reset the layout to its empty state."""
    del args
    app.control.request_answer_now()
    app.reset_view()
    return True


def _run_exit(app: t.Any, args: str) -> bool:
    """Quit the explorer."""
    del args
    app.app.exit()
    return True


def _run_help(app: t.Any, args: str) -> bool:
    """Show the available slash commands and their descriptions as a notification."""
    del args
    lines = [f"{_command_label(cmd)} — {cmd.description}" for cmd in app.slash_commands]
    app.notify("\n".join(lines), title="Slash commands", timeout=10)
    return True


def _run_keys(app: t.Any, args: str) -> bool:
    """Show the active layout bindings without mounting another screen."""
    del args
    app.notify_key_bindings()
    return True


def _run_theme(app: t.Any, args: str) -> bool:
    """Toggle or select one of agentgrep's two themes."""
    return bool(app.select_theme(args))


def _command_label(cmd: SlashCommand) -> str:
    """Render ``/name (/alias1, /alias2)`` for menus and help (argparse-style)."""
    label = f"/{command_menu_label(cmd)}"
    if cmd.aliases:
        label += " (" + ", ".join(f"/{alias}" for alias in cmd.aliases) + ")"
    return label


def command_menu_label(command: SlashCommand) -> str:
    """Return compact display text for one command without a leading slash."""
    if command.argument_hint:
        return f"{command.name} {command.argument_hint}"
    return command.name


SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("clear", ("new", "reset"), "Clear search and results", _run_clear),
    SlashCommand("exit", ("quit",), "Quit agentgrep", _run_exit),
    SlashCommand("help", (), "List slash commands", _run_help),
    SlashCommand("keys", (), "List active key bindings", _run_keys),
    SlashCommand(
        "theme",
        (),
        "Toggle or select the color theme",
        _run_theme,
        "[dark|light]",
        accepts_args=True,
    ),
)
"""The ordered command registry — drives both the ``/`` menu and dispatch."""


_COMMAND_BY_TOKEN: dict[str, SlashCommand] = {
    token: cmd for cmd in SLASH_COMMANDS for token in (cmd.name, *cmd.aliases)
}
"""Flat token -> command lookup; one entry per name and per alias."""


def resolve_command(
    token: str,
    slash_commands: tuple[SlashCommand, ...] = SLASH_COMMANDS,
) -> SlashCommand | None:
    """Resolve a command token (alias-aware) to its record, or ``None``.

    Tolerates a leading ``/`` and any case, so ``/Clear``, ``new``, and
    ``reset`` all resolve to the same record.
    """
    normalized = token.lower().lstrip("/")
    if slash_commands is SLASH_COMMANDS:
        return _COMMAND_BY_TOKEN.get(normalized)
    return next(
        (command for command in slash_commands if normalized in (command.name, *command.aliases)),
        None,
    )


def parse_command(text: str) -> tuple[str, str]:
    """Split a ``/command args`` line into ``(lowercased token, trimmed args)``.

    The leading ``/`` is dropped; the token is everything up to the first space,
    the args everything after. A bare ``/`` parses to ``("", "")``.
    """
    body = text.strip().removeprefix("/")
    token, _, args = body.partition(" ")
    return token.lower(), args.strip()


def command_matches(
    prefix: str,
    slash_commands: tuple[SlashCommand, ...] = SLASH_COMMANDS,
) -> tuple[SlashCommand, ...]:
    """Return the commands whose name or any alias starts with ``prefix``.

    A bare/empty prefix lists every command; each command appears at most once
    even when several of its tokens share the prefix.
    """
    needle = prefix.lower().lstrip("/")
    return tuple(
        cmd
        for cmd in slash_commands
        if any(token.startswith(needle) for token in (cmd.name, *cmd.aliases))
    )
