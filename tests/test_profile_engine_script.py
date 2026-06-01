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


def _write_codex_history(
    home: pathlib.Path,
    *,
    text: str,
) -> pathlib.Path:
    """Write a synthetic Codex prompt-history file the profiler can discover."""
    path = home / ".codex" / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"session_id": "history-session", "ts": 1_700_000_000, "text": text}
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


def _find_profile_run(payload: dict[str, object]) -> dict[str, object]:
    """Return the find-prompts child payload from a batch profile."""
    runs = t.cast("list[dict[str, object]]", payload["runs"])
    return next(run for run in runs if run["profile_component"] == "find-prompts")


def _sample_payload() -> dict[str, object]:
    """Return a small sanitized profile payload for renderer tests."""
    return {
        "kind": "search",
        "profile_command": "search",
        "profile_component": "search-prompts",
        "agent_count": 1,
        "term_count": 1,
        "limit": 1,
        "result_count": 1,
        "discovered_source_count": 2,
        "planned_source_count": 1,
        "scope": "prompts",
        "profile": {
            "samples": [
                {
                    "name": "search.discover",
                    "duration_seconds": 0.1,
                    "attributes": {"agentgrep_source_count": 2},
                },
                {
                    "name": "search.collect",
                    "duration_seconds": 1.2,
                    "attributes": {"agentgrep_source_count": 1},
                },
            ],
        },
    }


class ProfileRenderCase(t.NamedTuple):
    """Expected renderer behavior for one profiler output format."""

    test_id: str
    output_format: str


PROFILE_RENDER_CASES: tuple[ProfileRenderCase, ...] = (
    ProfileRenderCase(test_id="json", output_format="json"),
    ProfileRenderCase(test_id="ndjson", output_format="ndjson"),
)


@pytest.mark.parametrize(
    "case",
    PROFILE_RENDER_CASES,
    ids=[c.test_id for c in PROFILE_RENDER_CASES],
)
def test_render_payload_machine_formats_preserve_profile_samples(
    case: ProfileRenderCase,
) -> None:
    """Machine renderers preserve the sanitized child profile payload."""
    payload = _sample_payload()

    rendered = profile_engine._render_payload(
        payload,
        output_format=case.output_format,
        top_spans=5,
    )

    lines = rendered.splitlines()
    if case.output_format == "json":
        decoded = json.loads(rendered)
        assert decoded == payload
    else:
        assert len(lines) == 1
        decoded = json.loads(lines[0])
        assert decoded["profile_component"] == "search-prompts"
    profile = t.cast("dict[str, object]", decoded["profile"])
    samples = t.cast("list[dict[str, object]]", profile["samples"])
    assert [sample["name"] for sample in samples] == ["search.discover", "search.collect"]


def test_render_payload_ndjson_expands_batch_to_one_line_per_component() -> None:
    """Batch NDJSON emits child profile runs, not one nested batch document."""
    payload = {
        "kind": "profile_batch",
        "profile_command": "all",
        "profile_component": "all",
        "runs": [
            _sample_payload(),
            {
                **_sample_payload(),
                "profile_command": "find",
                "profile_component": "find-prompts",
            },
        ],
    }

    rendered = profile_engine._render_payload(payload, output_format="ndjson", top_spans=5)
    lines = rendered.splitlines()

    assert len(lines) == 2
    assert [json.loads(line)["profile_component"] for line in lines] == [
        "search-prompts",
        "find-prompts",
    ]


def test_render_payload_rich_reports_top_spans_without_sensitive_text() -> None:
    """The rich renderer gives a readable top-spans view from sanitized payloads."""
    payload = _sample_payload()

    rendered = profile_engine._render_payload(payload, output_format="rich", top_spans=1)

    assert "profile summary" in rendered
    assert "slowest spans" in rendered
    assert "search-prompts" in rendered
    assert "search.collect" in rendered
    assert "search.discover" not in rendered
    assert "private-token" not in rendered


def test_fmt_attributes_drops_denied_keys_and_keeps_safe_classifiers() -> None:
    """Rich attribute cells deny argv/command/path/query keys as defense in depth.

    Payloads are sanitized at construction, so this guards against a
    future attribute addition leaking into terminal output. Classifier
    keys that merely contain "path" in the name stay visible.
    """
    rendered = profile_engine._fmt_attributes(
        {
            "agentgrep_path": "/home/private/project",
            "agentgrep_query": "private-token",
            "agentgrep_path_kind": "sqlite_db",
            "agentgrep_source_count": 2,
        },
    )

    assert rendered == "agentgrep_path_kind=sqlite_db, agentgrep_source_count=2"


def test_profile_main_honors_ndjson_format(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The script entry point supports pipe-friendly NDJSON output."""
    _ = _write_codex_session(tmp_path, name="match.jsonl", text="tmux prompt")
    monkeypatch.setenv("HOME", str(tmp_path))

    exit_code = profile_engine.main(
        ["grep-prompts", "tmux", "--agent", "codex", "--max-count", "1", "--format", "ndjson"],
    )

    assert exit_code == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["profile_component"] == "grep-prompts"


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


def test_profile_all_does_not_apply_content_terms_to_find_prompts(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The all component keeps content terms out of prompt-source enumeration."""
    _ = _write_codex_history(tmp_path, text="tmux prompt")
    monkeypatch.setenv("HOME", str(tmp_path))
    parser = profile_engine._build_parser()

    all_args = parser.parse_args(["all", "tmux", "--agent", "codex", "--limit", "500"])
    find_args = parser.parse_args(["find-prompts", "--agent", "codex", "--limit", "500"])

    find_run = _find_profile_run(profile_engine._run(all_args))
    standalone = profile_engine._run(find_args)

    assert find_run["result_count"] == standalone["result_count"] == 1
    assert find_run["term_count"] == 0
    assert standalone["term_count"] == 0


def test_profile_legacy_find_applies_terms_as_source_pattern(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy find component still uses terms as source metadata filters."""
    _ = _write_codex_history(tmp_path, text="tmux prompt")
    monkeypatch.setenv("HOME", str(tmp_path))
    parser = profile_engine._build_parser()
    args = parser.parse_args(["find", "tmux", "--agent", "codex", "--type", "prompts"])

    payload = profile_engine._run(args)

    assert payload["profile_component"] == "find"
    assert payload["result_count"] == 0
    assert payload["term_count"] == 1


def test_profile_rejects_conflicting_limit_aliases() -> None:
    """--limit and --max-count are aliases; conflicting values fail loud."""
    parser = profile_engine._build_parser()
    args = parser.parse_args(["grep-prompts", "tmux", "--limit", "1", "--max-count", "2"])

    with pytest.raises(ValueError, match="--limit and --max-count disagree"):
        _ = profile_engine._resolve_result_limit(args)
