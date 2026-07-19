"""Tests for the slash-command registry (``ui/commands.py``).

The registry, alias resolution, command parsing, and prefix filter are
Textual-free pure functions, so they are exercised here directly. The handler
*effects* (``/clear`` resetting the explorer, ``/help`` notifying) are covered by
the app-level integration tests, since they act through the running app.
"""

from __future__ import annotations

import pytest

from agentgrep.ui import commands

pytestmark = pytest.mark.tui


def test_registry_has_common_layout_commands() -> None:
    """The common registry exposes the compact cross-layout slash surface."""
    by_name = {cmd.name: cmd for cmd in commands.SLASH_COMMANDS}
    assert {"clear", "exit", "help", "keys", "screenshot", "theme"} <= set(by_name)
    assert by_name["clear"].aliases == ("new", "reset")
    assert by_name["exit"].aliases == ("quit",)
    assert by_name["help"].aliases == ()
    assert by_name["theme"].argument_hint == "[name]"
    assert by_name["theme"].accepts_args is True
    assert by_name["screenshot"].aliases == ()
    assert by_name["screenshot"].accepts_args is False


def test_argument_hint_and_acceptance_are_independent_metadata() -> None:
    """Menu copy never silently changes whether a command consumes arguments."""
    hinted = commands.SlashCommand(
        name="hinted",
        aliases=(),
        description="Hint only",
        run=lambda _host, _args: True,
        argument_hint="[VALUE]",
    )
    accepting = commands.SlashCommand(
        name="accepting",
        aliases=(),
        description="Accept only",
        run=lambda _host, _args: True,
        accepts_args=True,
    )

    assert hinted.accepts_args is False
    assert accepting.argument_hint == ""


def test_resolve_command_by_name_and_aliases() -> None:
    """A command resolves by its name and by every alias to the same record."""
    clear = commands.resolve_command("clear")
    assert clear is not None
    assert clear.name == "clear"
    assert commands.resolve_command("new") is clear
    assert commands.resolve_command("reset") is clear
    # A leading slash and surrounding case are tolerated.
    assert commands.resolve_command("/clear") is clear
    assert commands.resolve_command("CLEAR") is clear
    assert commands.resolve_command("nope") is None
    # /exit and its /quit alias resolve to the one record.
    exit_command = commands.resolve_command("exit")
    assert exit_command is not None
    assert commands.resolve_command("quit") is exit_command


def test_parse_command_splits_token_and_args() -> None:
    """``parse_command`` returns the lowercased token and the trimmed remainder."""
    assert commands.parse_command("/clear") == ("clear", "")
    assert commands.parse_command("/clear extra args") == ("clear", "extra args")
    assert commands.parse_command("/HELP") == ("help", "")
    assert commands.parse_command("/") == ("", "")
    assert commands.parse_command("/   ") == ("", "")


def test_command_matches_prefix_filters() -> None:
    """A bare slash lists all; a prefix narrows by name or alias; junk is empty."""
    everything = {cmd.name for cmd in commands.command_matches("")}
    assert {"clear", "help"} <= everything
    assert [cmd.name for cmd in commands.command_matches("cl")] == ["clear"]
    assert [cmd.name for cmd in commands.command_matches("he")] == ["help"]
    assert [cmd.name for cmd in commands.command_matches("scr")] == ["screenshot"]
    # An alias prefix matches its command (no duplicate rows).
    assert [cmd.name for cmd in commands.command_matches("re")] == ["clear"]
    assert commands.command_matches("zzz") == ()


def test_command_matches_each_command_once() -> None:
    """A command is listed once even when several of its tokens share a prefix."""
    matched = commands.command_matches("")
    assert len(matched) == len(set(matched))


def test_resolution_and_matching_accept_layout_extensions() -> None:
    """A layout can extend the common registry without mutating it globally."""
    bookmark = commands.SlashCommand(
        name="bookmark",
        aliases=(),
        description="Toggle bookmark",
        run=lambda _host, _args: True,
    )
    registry = (*commands.SLASH_COMMANDS, bookmark)

    assert commands.resolve_command("bookmark", registry) is bookmark
    assert commands.command_matches("book", registry) == (bookmark,)


def test_command_menu_label_includes_argument_hint() -> None:
    """The menu displays argument guidance without using it as dispatch policy."""
    theme = commands.resolve_command("theme")
    assert theme is not None
    assert commands.command_menu_label(theme) == "theme [name]"


def test_zoom_commands_use_layout_safe_named_hooks() -> None:
    """Zoom metadata stays layout-aware and dispatch avoids Screen methods."""
    calls: list[tuple[str, str]] = []

    class Host:
        def handle_maximize_command(self, argument: str) -> bool:
            calls.append(("maximize", argument))
            return True

        def handle_minimize_command(self) -> bool:
            calls.append(("minimize", ""))
            return True

    maximize, minimize = commands.zoom_commands("[results|detail]")

    assert commands.command_menu_label(maximize) == "maximize [results|detail]"
    assert maximize.accepts_args is True
    assert minimize.accepts_args is False
    assert maximize.run(Host(), "detail") is True
    assert minimize.run(Host(), "") is True
    assert calls == [("maximize", "detail"), ("minimize", "")]
