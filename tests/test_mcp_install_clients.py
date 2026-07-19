"""Unit tests for the Grok CLI and Antigravity MCP-install clients.

These are pure-unit tests over ``docs._ext.widgets.mcp_install`` — no
Sphinx build — so they follow the in-function import pattern used by
``test_widgets.py`` (the repo root is not on ``sys.path`` at collection
time).
"""

from __future__ import annotations

import pathlib
import sys
import typing as t

import pytest

pytestmark = [pytest.mark.mcp, pytest.mark.setup]


def _load_mcp_install() -> t.Any:
    """Import the widget module, ensuring the repo root is importable."""
    repo = pathlib.Path(__file__).resolve().parents[1]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from docs._ext.widgets import mcp_install

    return mcp_install


def test_grok_and_antigravity_registered_with_expected_scopes() -> None:
    """Grok (CLI, user+project) and Antigravity (JSON, single global) register.

    Locks the config-file paths so a future edit can't silently regress
    them — in particular Antigravity's ``~/.gemini/config/mcp_config.json``,
    the file the ``agy`` binary actually reads (it has no ``mcp add`` verb).
    """
    m = _load_mcp_install()
    grok = next(c for c in m.CLIENTS if c.id == "grok")
    assert grok.label == "Grok CLI"
    assert grok.kind == "cli"
    assert [s.id for s in grok.scopes] == ["user", "project"]
    assert grok.scopes[0].config_file == "~/.grok/config.toml"
    assert grok.scopes[1].config_file == "./.grok/config.toml (in repo)"

    agy = next(c for c in m.CLIENTS if c.id == "antigravity")
    assert agy.label == "Antigravity"
    assert agy.kind == "json"
    assert [s.id for s in agy.scopes] == ["global"]
    assert agy.scopes[0].config_file == "~/.gemini/config/mcp_config.json"


class GrokBodyCase(t.NamedTuple):
    """One ``_body_for`` expectation for the Grok CLI client."""

    test_id: str
    method_id: str
    scope_id: str
    cooldown_id: str
    expected: str


_GROK_BODY_CASES: list[GrokBodyCase] = [
    GrokBodyCase(
        test_id="uvx-user-off",
        method_id="uvx",
        scope_id="user",
        cooldown_id="off",
        expected="grok mcp add --scope user agentgrep -- uvx --from agentgrep agentgrep-mcp",
    ),
    GrokBodyCase(
        test_id="uvx-project-off",
        method_id="uvx",
        scope_id="project",
        cooldown_id="off",
        expected="grok mcp add --scope project agentgrep -- uvx --from agentgrep agentgrep-mcp",
    ),
    GrokBodyCase(
        test_id="pipx-user-off",
        method_id="pipx",
        scope_id="user",
        cooldown_id="off",
        expected="grok mcp add --scope user agentgrep -- pipx run --spec agentgrep agentgrep-mcp",
    ),
    GrokBodyCase(
        test_id="pip-user-off",
        method_id="pip",
        scope_id="user",
        cooldown_id="off",
        expected="grok mcp add --scope user agentgrep -- agentgrep-mcp",
    ),
    GrokBodyCase(
        test_id="uvx-user-days",
        method_id="uvx",
        scope_id="user",
        cooldown_id="days",
        expected=(
            "grok mcp add --scope user agentgrep -- uvx --exclude-newer"
            " <COOLDOWN_DURATION> --exclude-newer-package agentgrep=2099-01-01"
            " --from agentgrep agentgrep-mcp"
        ),
    ),
    GrokBodyCase(
        test_id="uvx-user-bypass",
        method_id="uvx",
        scope_id="user",
        cooldown_id="bypass",
        expected=(
            "grok mcp add --scope user agentgrep -- uvx --no-config --from agentgrep agentgrep-mcp"
        ),
    ),
]


@pytest.mark.parametrize(
    list(GrokBodyCase._fields),
    _GROK_BODY_CASES,
    ids=[c.test_id for c in _GROK_BODY_CASES],
)
def test_body_for_grok_cli(
    test_id: str,
    method_id: str,
    scope_id: str,
    cooldown_id: str,
    expected: str,
) -> None:
    """Grok renders ``grok mcp add --scope <s> agentgrep -- <cmd>`` for both scopes.

    Unlike Codex, Grok's CLI writes both user and project scopes itself
    (``~/.grok/config.toml`` / ``./.grok/config.toml``), so there is no
    manual-TOML-paste cell — every Grok panel is a ``console`` command.
    """
    m = _load_mcp_install()
    grok = next(c for c in m.CLIENTS if c.id == "grok")
    method = next(x for x in m.METHODS if x.id == method_id)
    scope = next(s for s in grok.scopes if s.id == scope_id)
    cooldown = next(c for c in m.COOLDOWNS if c.id == cooldown_id)
    body, language, _ = m._body_for(grok, method, scope, cooldown)
    assert body == expected
    assert language == "console"


class AntigravityBodyCase(t.NamedTuple):
    """One ``_body_for`` expectation for the Antigravity (agy) JSON client."""

    test_id: str
    method_id: str
    cooldown_id: str
    expected_substrings: tuple[str, ...]


_ANTIGRAVITY_BODY_CASES: list[AntigravityBodyCase] = [
    AntigravityBodyCase(
        test_id="uvx-off",
        method_id="uvx",
        cooldown_id="off",
        expected_substrings=('"mcpServers"', '"command": "uvx"', '"agentgrep-mcp"'),
    ),
    AntigravityBodyCase(
        test_id="uvx-days",
        method_id="uvx",
        cooldown_id="days",
        expected_substrings=('"--exclude-newer"', '"<COOLDOWN_DURATION>"'),
    ),
    AntigravityBodyCase(
        test_id="uvx-bypass",
        method_id="uvx",
        cooldown_id="bypass",
        expected_substrings=('"env":', '"UV_NO_CONFIG": "1"'),
    ),
]


@pytest.mark.parametrize(
    list(AntigravityBodyCase._fields),
    _ANTIGRAVITY_BODY_CASES,
    ids=[c.test_id for c in _ANTIGRAVITY_BODY_CASES],
)
def test_body_for_antigravity_json(
    test_id: str,
    method_id: str,
    cooldown_id: str,
    expected_substrings: tuple[str, ...],
) -> None:
    """Antigravity reuses the JSON ``mcpServers`` body — ``agy`` has no ``mcp add``.

    The rendered snippet is what the user pastes into
    ``~/.gemini/config/mcp_config.json`` (single global scope).
    """
    m = _load_mcp_install()
    agy = next(c for c in m.CLIENTS if c.id == "antigravity")
    method = next(x for x in m.METHODS if x.id == method_id)
    cooldown = next(c for c in m.COOLDOWNS if c.id == cooldown_id)
    body, language, _ = m._body_for(agy, method, agy.scopes[0], cooldown)
    assert language == "json"
    for needle in expected_substrings:
        assert needle in body
