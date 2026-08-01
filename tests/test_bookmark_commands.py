# ruff: noqa: D103
"""Focused command contracts for the TUI bookmark workflow."""

from __future__ import annotations

import dataclasses

import pytest

from agentgrep.ui import commands

pytestmark = pytest.mark.tui


@dataclasses.dataclass
class _BookmarkCommandApp:
    """Tiny command-handler host recording bookmark effects."""

    toggles: list[str] = dataclasses.field(default_factory=list)
    opens: int = 0
    notes: list[str] = dataclasses.field(default_factory=list)

    def toggle_bookmark(self, scope: str) -> None:
        self.toggles.append(scope)

    def open_bookmarks(self) -> None:
        self.opens += 1

    def notify(self, message: str, **_kwargs: object) -> None:
        self.notes.append(message)


def test_bookmark_commands_are_hud_extensions() -> None:
    by_name = {command.name: command for command in commands.bookmark_commands()}

    assert set(by_name) == {"bookmark", "bookmarks"}
    assert by_name["bookmark"].argument_hint == "[record|thread|content]"
    assert by_name["bookmark"].accepts_args is True
    assert by_name["bookmarks"].argument_hint == ""
    assert commands.command_menu_label(by_name["bookmark"]) == ("bookmark [record|thread|content]")
    assert commands.command_menu_label(by_name["bookmarks"]) == "bookmarks"


def test_bookmark_command_defaults_and_accepts_scopes() -> None:
    command = commands.resolve_command("bookmark", commands.bookmark_commands())
    assert command is not None
    app = _BookmarkCommandApp()

    for raw in ("", "record", "thread", "content"):
        command.run(app, raw)

    assert app.toggles == ["record", "record", "thread", "content"]
    assert app.notes == []


def test_bookmark_command_rejects_invalid_scope() -> None:
    command = commands.resolve_command("bookmark", commands.bookmark_commands())
    assert command is not None
    app = _BookmarkCommandApp()

    command.run(app, "conversation")

    assert app.toggles == []
    assert app.notes == ["Bookmark scope must be record, thread, or content."]


def test_bookmarks_command_opens_recall() -> None:
    command = commands.resolve_command("bookmarks", commands.bookmark_commands())
    assert command is not None
    app = _BookmarkCommandApp()

    command.run(app, "")

    assert app.opens == 1
