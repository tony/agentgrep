"""Tests for the slash-command registry (``ui/commands.py``).

The registry, alias resolution, command parsing, and prefix filter are
Textual-free pure functions, so they are exercised here directly. The handler
*effects* (``/clear`` resetting the explorer, ``/help`` notifying) are covered by
the app-level integration tests, since they act through the running app.
"""

from __future__ import annotations

from agentgrep.ui import commands


def test_registry_has_clear_exit_and_help() -> None:
    """The registry ships ``/clear``, ``/exit``, and ``/help`` with their aliases."""
    by_name = {cmd.name: cmd for cmd in commands.SLASH_COMMANDS}
    assert {"clear", "exit", "help"} <= set(by_name)
    assert by_name["clear"].aliases == ("new", "reset")
    assert by_name["exit"].aliases == ("quit",)
    assert by_name["help"].aliases == ()


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
    # An alias prefix matches its command (no duplicate rows).
    assert [cmd.name for cmd in commands.command_matches("re")] == ["clear"]
    assert commands.command_matches("zzz") == ()


def test_command_matches_each_command_once() -> None:
    """A command is listed once even when several of its tokens share a prefix."""
    matched = commands.command_matches("")
    assert len(matched) == len(set(matched))
