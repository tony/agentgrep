"""Tests for bounded TUI export preferences and filename compilation."""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import stat
import typing as t

import pytest

from agentgrep.ui import _export_preferences as export_preferences
from agentgrep.ui._export_preferences import (
    DEFAULT_FILENAME_TEMPLATE,
    MAX_FILENAME_BYTES,
    MAX_PREFERENCES_BYTES,
    MAX_TEMPLATE_CHARS,
    ExportPreferences,
    ExportPreferencesError,
    default_export_directory,
    export_preferences_path,
    load_export_preferences,
    render_export_filename,
    resolve_export_directory,
    save_export_preferences,
)

DEFAULT_TEMPLATE = "{date} {time} - {title}.md"


def test_export_preferences_path_follows_xdg_config_home(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TUI-private file follows the configured XDG config root."""
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    assert export_preferences_path(tmp_path / "home") == (
        config_home / "agentgrep" / "tui-export.json"
    )


def test_export_preferences_path_falls_back_under_home(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The config path uses the standard home fallback without XDG config."""
    home = tmp_path / "home"
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    assert export_preferences_path(home) == (home / ".config" / "agentgrep" / "tui-export.json")


def test_default_export_directory_follows_xdg_data_home(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default destination follows the configured XDG data root."""
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    assert default_export_directory(tmp_path / "home") == (data_home / "agentgrep" / "exports")


def test_default_export_directory_falls_back_under_home(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default destination uses the standard home fallback."""
    home = tmp_path / "home"
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    assert default_export_directory(home) == (home / ".local" / "share" / "agentgrep" / "exports")


@pytest.mark.parametrize(
    ("value", "suffix"),
    (
        ("~", ()),
        ("~/", ()),
        ("~/Exports", ("Exports",)),
        ("~/Exports/agentgrep", ("Exports", "agentgrep")),
    ),
)
def test_resolve_export_directory_expands_only_current_home(
    value: str,
    suffix: tuple[str, ...],
    tmp_path: pathlib.Path,
) -> None:
    """A bare or slash-suffixed tilde expands against the supplied home."""
    home = tmp_path / "home"

    assert resolve_export_directory(value, home) == home.joinpath(*suffix)


def test_resolve_export_directory_preserves_non_tilde_path(
    tmp_path: pathlib.Path,
) -> None:
    """Absolute and relative paths do not receive implicit expansion."""
    absolute = tmp_path / "Exports"

    assert resolve_export_directory(str(absolute), tmp_path / "home") == absolute
    assert resolve_export_directory("relative/exports", tmp_path / "home") == pathlib.Path(
        "relative/exports"
    )


def test_resolve_export_directory_rejects_other_users(
    tmp_path: pathlib.Path,
) -> None:
    """Other-user tilde syntax is never delegated to account lookup."""
    with pytest.raises(ExportPreferencesError):
        resolve_export_directory("~other/Exports", tmp_path / "home")


def test_default_export_filename_is_frozen_local_ascii() -> None:
    """The default template compiles to the reviewed local-time basename."""
    when = datetime.datetime(2026, 7, 14, 9, 8, 7).astimezone()
    assert (
        render_export_filename(
            DEFAULT_TEMPLATE,
            title="Refactor: Planner / Review",
            fallback_title="codex-prompt",
            timestamp=when,
        )
        == "2026-07-14 09-08-07 - refactor-planner-review.md"
    )


@pytest.mark.parametrize(
    "template",
    (
        "{unknown}.md",
        "../{title}.md",
        "{title}/body.md",
        "{title}",
        ".md",
        "CON.md",
    ),
)
def test_export_filename_rejects_unreviewable_names(template: str) -> None:
    """Unsafe, unsupported, or extensionless output names are rejected."""
    with pytest.raises(ExportPreferencesError):
        render_export_filename(
            template,
            title="Title",
            fallback_title="codex-prompt",
            timestamp=datetime.datetime(2026, 7, 14).astimezone(),
        )


@pytest.mark.parametrize(
    "template",
    (
        "{{title}}.md",
        "{title}.md ",
        "{title}.md.",
        "{title}\n.md",
        "\ud800.md",
    ),
)
def test_export_filename_rejects_ambiguous_or_non_scalar_output(template: str) -> None:
    """Braces, trailing ambiguity, controls, and surrogates are rejected."""
    with pytest.raises(ExportPreferencesError):
        render_export_filename(
            template,
            title="Title",
            fallback_title="codex-prompt",
            timestamp=datetime.datetime(2026, 7, 14).astimezone(),
        )


def test_export_filename_normalizes_unicode_and_uses_sanitized_fallback() -> None:
    """Unicode letters survive while separators collapse and empty titles fall back."""
    when = datetime.datetime(2026, 7, 14).astimezone()

    assert (
        render_export_filename(
            "{title}.md",
            title="  Crème 🚀 東京  ",
            fallback_title="codex-prompt",
            timestamp=when,
        )
        == "crème-東京.md"
    )
    assert (
        render_export_filename(
            "{title}.md",
            title="///",
            fallback_title="Codex Prompt",
            timestamp=when,
        )
        == "codex-prompt.md"
    )


def test_export_filename_slices_raw_title_before_nfkc_normalization() -> None:
    """Normalization cannot pull title content from beyond the raw bound."""
    title = "-" * 255 + "Ⅳ" + "B"

    assert (
        render_export_filename(
            "{title}.md",
            title=title,
            fallback_title="codex-prompt",
            timestamp=datetime.datetime(2026, 7, 14).astimezone(),
        )
        == "iv.md"
    )


def test_export_filename_rejects_template_and_utf8_filename_over_bounds() -> None:
    """Both editable templates and compiled UTF-8 names have fixed bounds."""
    with pytest.raises(ExportPreferencesError):
        render_export_filename(
            "x" * (MAX_TEMPLATE_CHARS + 1) + ".md",
            title="Title",
            fallback_title="codex-prompt",
            timestamp=datetime.datetime(2026, 7, 14).astimezone(),
        )

    with pytest.raises(ExportPreferencesError):
        render_export_filename(
            "{title}.md",
            title="é" * MAX_FILENAME_BYTES,
            fallback_title="codex-prompt",
            timestamp=datetime.datetime(2026, 7, 14).astimezone(),
        )


def test_missing_export_preferences_return_defaults_without_warning(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent private config is a normal first-run state."""
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    loaded = load_export_preferences(tmp_path / "home")

    assert loaded.preferences == ExportPreferences(
        directory=str(data_home / "agentgrep" / "exports"),
        filename_template=DEFAULT_FILENAME_TEMPLATE,
    )
    assert loaded.warning is None


@pytest.mark.parametrize(
    "payload",
    (
        b"{",
        b" " * (MAX_PREFERENCES_BYTES + 1),
        b'{"version":2,"directory":"~/Exports","filename_template":"{title}.md"}',
        b'{"version":true,"directory":"~/Exports","filename_template":"{title}.md"}',
        b'{"version":1,"directory":[],"filename_template":"{title}.md"}',
        b'{"version":1,"directory":"~/Exports","filename_template":2}',
        b'{"version":1,"directory":"~/Exports","filename_template":"{title}.md","extra":1}',
        b'{"version":1,"version":1,"directory":"~/Exports","filename_template":"{title}.md"}',
    ),
)
def test_invalid_export_preferences_return_defaults_with_warning(
    payload: bytes,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed, oversized, or non-exact schemas degrade to safe defaults."""
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    config_path = config_home / "agentgrep" / "tui-export.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(payload)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    loaded = load_export_preferences(tmp_path / "home")

    assert loaded.preferences == ExportPreferences(
        directory=str(data_home / "agentgrep" / "exports")
    )
    assert loaded.warning == "Export preferences could not be read"


def test_export_preferences_round_trip_unicode_with_private_modes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Saving preserves Unicode and fixes app-owned config permissions."""
    config_home = tmp_path / "config"
    config_home.mkdir(mode=0o755)
    config_home.chmod(0o755)
    app_config = config_home / "agentgrep"
    app_config.mkdir(mode=0o755)
    app_config.chmod(0o755)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    preferences = ExportPreferences(
        directory="~/Éxports/東京",
        filename_template="{date} — {title}.md",
    )

    save_export_preferences(tmp_path / "home", preferences)

    config_path = export_preferences_path(tmp_path / "home")
    assert load_export_preferences(tmp_path / "home").preferences == preferences
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(app_config.stat().st_mode) == 0o700
    assert stat.S_IMODE(config_home.stat().st_mode) == 0o755
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "directory": "~/Éxports/東京",
        "filename_template": "{date} — {title}.md",
    }


def test_save_export_preferences_retries_short_writes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful atomic save drains every short write."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    real_write = os.write
    write_sizes: list[int] = []

    def short_write(fd: int, data: bytes | bytearray | memoryview) -> int:
        chunk = data[:7]
        write_sizes.append(len(chunk))
        return real_write(fd, chunk)

    monkeypatch.setattr(export_preferences.os, "write", short_write)
    preferences = ExportPreferences(
        directory="~/" + "É" * 100,
        filename_template=DEFAULT_TEMPLATE,
    )

    save_export_preferences(tmp_path / "home", preferences)

    assert len(write_sizes) > 1
    assert load_export_preferences(tmp_path / "home").preferences == preferences


def test_save_export_preferences_cleans_temp_after_write_failure(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed atomic write leaves no temporary or destination file."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    def fail_write(_fd: int, _data: bytes | bytearray | memoryview) -> t.NoReturn:
        raise OSError

    monkeypatch.setattr(export_preferences.os, "write", fail_write)

    with pytest.raises(ExportPreferencesError) as raised:
        save_export_preferences(
            tmp_path / "home",
            ExportPreferences(directory="~/Exports"),
        )

    app_config = config_home / "agentgrep"
    assert list(app_config.iterdir()) == []
    assert str(config_home) not in str(raised.value)


def test_save_export_preferences_never_chmods_selected_directory(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Saving config changes no permissions on the user-selected destination."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    selected = tmp_path / "Selected"
    selected.mkdir(mode=0o750)
    selected.chmod(0o750)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    save_export_preferences(
        tmp_path / "home",
        ExportPreferences(directory=str(selected)),
    )

    assert stat.S_IMODE(selected.stat().st_mode) == 0o750
