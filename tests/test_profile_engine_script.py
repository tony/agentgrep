"""Tests for scripts/profile_engine.py."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import typing as t

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "profile_engine.py"

_spec = importlib.util.spec_from_file_location("profile_engine_script", _SCRIPT)
assert _spec and _spec.loader
profile_engine = importlib.util.module_from_spec(_spec)
sys.modules["profile_engine_script"] = profile_engine
_spec.loader.exec_module(profile_engine)


def _write_codex_session(
    home: pathlib.Path,
    *,
    name: str,
    text: str,
) -> pathlib.Path:
    """Write a synthetic Codex session-jsonl file the engine can parse."""
    path = home / ".codex" / "sessions" / "2026" / "05" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"type": "response_item", "payload": {"role": "user", "content": text}}
    path.write_text(json.dumps(payload) + "\n")
    return path


class ProfileComponentCase(t.NamedTuple):
    """Expected profiler expansion for one component argument."""

    test_id: str
    component: str
    expected_components: tuple[str, ...]
    expected_commands: tuple[str, ...]


PROFILE_COMPONENT_CASES: tuple[ProfileComponentCase, ...] = (
    ProfileComponentCase(
        test_id="single-search-prompts",
        component="search-prompts",
        expected_components=("search-prompts",),
        expected_commands=("search",),
    ),
    ProfileComponentCase(
        test_id="single-grep-conversations",
        component="grep-conversations",
        expected_components=("grep-conversations",),
        expected_commands=("grep",),
    ),
    ProfileComponentCase(
        test_id="all-components",
        component="all",
        expected_components=(
            "search-prompts",
            "search-conversations",
            "grep-prompts",
            "grep-conversations",
            "find-prompts",
        ),
        expected_commands=("search", "search", "grep", "grep", "find"),
    ),
)


class ProfileScopeCase(t.NamedTuple):
    """Expected search scope for one profiler component run."""

    test_id: str
    component: str
    cli_scope: str
    expected_scope: str


PROFILE_SCOPE_CASES: tuple[ProfileScopeCase, ...] = (
    ProfileScopeCase(
        test_id="conversation-component-overrides-default",
        component="search-conversations",
        cli_scope="prompts",
        expected_scope="conversations",
    ),
    ProfileScopeCase(
        test_id="grep-prompt-component-overrides-cli",
        component="grep-prompts",
        cli_scope="conversations",
        expected_scope="prompts",
    ),
    ProfileScopeCase(
        test_id="legacy-search-uses-cli-scope",
        component="search",
        cli_scope="conversations",
        expected_scope="conversations",
    ),
)


def _profile_scopes(payload: dict[str, object]) -> tuple[str, ...]:
    """Return profile sample scopes from a profiler payload."""
    profile = t.cast("dict[str, object]", payload["profile"])
    samples = t.cast("list[dict[str, object]]", profile["samples"])
    scopes: list[str] = []
    for sample in samples:
        attributes = t.cast("dict[str, object]", sample["attributes"])
        scope = attributes.get("agentgrep_scope")
        if isinstance(scope, str):
            scopes.append(scope)
    return tuple(scopes)


@pytest.mark.parametrize(
    "case",
    PROFILE_COMPONENT_CASES,
    ids=[c.test_id for c in PROFILE_COMPONENT_CASES],
)
def test_profile_component_specs_expand_to_expected_runs(case: ProfileComponentCase) -> None:
    """Profiler component arguments expand to stable command runs."""
    parser = profile_engine._build_parser()
    args = parser.parse_args([case.component, "secret-query", "--agent", "codex", "--limit", "1"])

    specs = profile_engine._resolve_component_specs(args)

    assert tuple(spec.component for spec in specs) == case.expected_components
    assert tuple(spec.command for spec in specs) == case.expected_commands


@pytest.mark.parametrize(
    "case",
    PROFILE_SCOPE_CASES,
    ids=[c.test_id for c in PROFILE_SCOPE_CASES],
)
def test_profile_components_use_expected_search_scope(
    case: ProfileScopeCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Component names pin prompt/conversation scope unless using legacy search."""
    monkeypatch.setenv("HOME", str(tmp_path))
    parser = profile_engine._build_parser()
    args = parser.parse_args(
        [
            case.component,
            "tmux",
            "--agent",
            "codex",
            "--scope",
            case.cli_scope,
            "--limit",
            "1",
        ],
    )

    payload = profile_engine._run(args)

    assert payload["scope"] == case.expected_scope
    scopes = _profile_scopes(payload)
    assert scopes
    assert set(scopes) == {case.expected_scope}


def test_profile_component_run_redacts_query_and_home(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile output reports counts and timings without query text or local paths."""
    _ = _write_codex_session(tmp_path, name="match.jsonl", text="private-token prompt")
    monkeypatch.setenv("HOME", str(tmp_path))
    parser = profile_engine._build_parser()
    args = parser.parse_args(
        ["grep-prompts", "private-token", "--agent", "codex", "--max-count", "1"],
    )

    payload = profile_engine._run(args)

    assert payload["profile_component"] == "grep-prompts"
    assert payload["profile_command"] == "grep"
    assert payload["result_count"] == 1
    assert payload["max_count"] == 1
    encoded = json.dumps(payload, sort_keys=True)
    assert "private-token" not in encoded
    assert str(tmp_path) not in encoded


def test_profile_all_runs_every_component(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The all component returns one sanitized payload per profiler component."""
    _ = _write_codex_session(tmp_path, name="match.jsonl", text="tmux prompt")
    monkeypatch.setenv("HOME", str(tmp_path))
    parser = profile_engine._build_parser()
    args = parser.parse_args(["all", "tmux", "--agent", "codex", "--limit", "1"])

    payload = profile_engine._run(args)

    assert payload["kind"] == "profile_batch"
    assert payload["profile_component"] == "all"
    runs = t.cast("list[dict[str, object]]", payload["runs"])
    assert [run["profile_component"] for run in runs] == [
        "search-prompts",
        "search-conversations",
        "grep-prompts",
        "grep-conversations",
        "find-prompts",
    ]
    assert [run.get("scope") for run in runs] == [
        "prompts",
        "conversations",
        "prompts",
        "conversations",
        None,
    ]
    assert runs[-1]["type_filter"] == "prompts"


def test_profile_rejects_conflicting_limit_aliases() -> None:
    """--limit and --max-count are aliases; conflicting values fail loud."""
    parser = profile_engine._build_parser()
    args = parser.parse_args(["grep-prompts", "tmux", "--limit", "1", "--max-count", "2"])

    with pytest.raises(ValueError, match="--limit and --max-count disagree"):
        _ = profile_engine._resolve_result_limit(args)
