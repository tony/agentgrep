"""Tests for scripts/profile_engine.py."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3
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


def _write_cursor_state_vscdb(
    home: pathlib.Path,
    *,
    text: str,
) -> pathlib.Path:
    """Write a synthetic Cursor IDE state database the engine can parse."""
    path = home / ".cursor" / "state.vscdb"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        _ = connection.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
        payload = {"messages": [{"role": "user", "content": text}]}
        _ = connection.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            ("workbench.panel.chat.composerData", json.dumps(payload)),
        )
        connection.commit()
    finally:
        connection.close()
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


class ProfileDefaultOutputCase(t.NamedTuple):
    """One profiler component that should default to rich terminal output."""

    test_id: str
    argv: tuple[str, ...]
    expected_component: str


PROFILE_DEFAULT_OUTPUT_CASES: tuple[ProfileDefaultOutputCase, ...] = (
    ProfileDefaultOutputCase(
        test_id="search-prompts",
        argv=("search-prompts", "--agent", "codex", "--limit", "1"),
        expected_component="search-prompts",
    ),
    ProfileDefaultOutputCase(
        test_id="search-conversations",
        argv=("search-conversations", "tmux", "--agent", "codex", "--limit", "1"),
        expected_component="search-conversations",
    ),
    ProfileDefaultOutputCase(
        test_id="grep-prompts",
        argv=("grep-prompts", "tmux", "--agent", "codex", "--max-count", "1"),
        expected_component="grep-prompts",
    ),
    ProfileDefaultOutputCase(
        test_id="grep-conversations",
        argv=("grep-conversations", "tmux", "--agent", "codex", "--max-count", "1"),
        expected_component="grep-conversations",
    ),
    ProfileDefaultOutputCase(
        test_id="find-prompts",
        argv=("find-prompts", "--agent", "codex", "--limit", "1"),
        expected_component="find-prompts",
    ),
    ProfileDefaultOutputCase(
        test_id="all",
        argv=("all", "tmux", "--agent", "codex", "--limit", "1"),
        expected_component="find-prompts",
    ),
    ProfileDefaultOutputCase(
        test_id="legacy-search",
        argv=("search", "tmux", "--agent", "codex", "--scope", "prompts", "--limit", "1"),
        expected_component="search",
    ),
    ProfileDefaultOutputCase(
        test_id="legacy-find",
        argv=("find", "--agent", "codex", "--type", "prompts", "--limit", "1"),
        expected_component="find",
    ),
)


class ProfileMachineShortcutCase(t.NamedTuple):
    """One machine-output shortcut flag for the profiler CLI."""

    test_id: str
    flag: str
    expected_line_count: int


PROFILE_MACHINE_SHORTCUT_CASES: tuple[ProfileMachineShortcutCase, ...] = (
    ProfileMachineShortcutCase(test_id="json", flag="--json", expected_line_count=1),
    ProfileMachineShortcutCase(test_id="ndjson", flag="--ndjson", expected_line_count=1),
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


def test_profile_payloads_include_artifact_metadata() -> None:
    """Single-run profile artifacts are self-describing for long-lived consumers."""
    parser = profile_engine._build_parser()
    args = parser.parse_args(["grep-prompts", "tmux", "--agent", "codex", "--max-count", "1"])

    payload = profile_engine._run(args)

    assert payload["schema_version"] == 1
    assert payload["artifact_kind"] == "agentgrep.profile.run"


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


def test_profile_batch_payload_includes_artifact_metadata(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batch profile artifacts carry root metadata while child runs stay typed."""
    _ = _write_codex_session(tmp_path, name="match.jsonl", text="tmux prompt")
    monkeypatch.setenv("HOME", str(tmp_path))
    parser = profile_engine._build_parser()
    args = parser.parse_args(["all", "tmux", "--agent", "codex", "--limit", "1"])

    payload = profile_engine._run(args)

    assert payload["schema_version"] == 1
    assert payload["artifact_kind"] == "agentgrep.profile.batch"
    runs = t.cast("list[dict[str, object]]", payload["runs"])
    assert {run["artifact_kind"] for run in runs} == {"agentgrep.profile.run"}


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


def test_render_payload_rich_reports_physical_strategy_groups() -> None:
    """Rich output gives physical-plan strategy counts without requiring jq."""
    payload = _sample_payload()
    profile = t.cast("dict[str, object]", payload["profile"])
    samples = t.cast("list[dict[str, object]]", profile["samples"])
    samples.append(
        {
            "name": "search.plan.strategy_group",
            "duration_seconds": 0.0,
            "attributes": {
                "agentgrep_agent": "codex",
                "agentgrep_store": "codex.sessions",
                "agentgrep_adapter_id": "codex.sessions_jsonl.v1",
                "agentgrep_path_kind": "session_file",
                "agentgrep_source_kind": "jsonl",
                "agentgrep_source_strategy": "jsonl_raw_text_prefilter",
                "agentgrep_source_count": 3,
            },
        },
    )

    rendered = profile_engine._render_payload(payload, output_format="rich", top_spans=0)

    assert "physical strategies" in rendered
    assert "jsonl_raw_text_prefilter" in rendered
    assert "codex.sessions_jsonl.v1" in rendered


@pytest.mark.parametrize(
    "case",
    PROFILE_DEFAULT_OUTPUT_CASES,
    ids=[c.test_id for c in PROFILE_DEFAULT_OUTPUT_CASES],
)
def test_profile_main_defaults_to_rich_output_for_components(
    case: ProfileDefaultOutputCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Profiler components default to human-readable rich output."""
    _ = _write_codex_history(tmp_path, text="tmux prompt")
    _ = _write_codex_session(tmp_path, name="match.jsonl", text="tmux prompt")
    monkeypatch.setenv("HOME", str(tmp_path))

    exit_code = profile_engine.main(list(case.argv))

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "profile summary" in output
    assert "slowest spans" in output
    assert case.expected_component in output
    assert not output.lstrip().startswith("{")


@pytest.mark.parametrize(
    "case",
    PROFILE_MACHINE_SHORTCUT_CASES,
    ids=[c.test_id for c in PROFILE_MACHINE_SHORTCUT_CASES],
)
def test_profile_main_honors_machine_format_shortcuts(
    case: ProfileMachineShortcutCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--json and --ndjson request machine output explicitly."""
    _ = _write_codex_session(tmp_path, name="match.jsonl", text="tmux prompt")
    monkeypatch.setenv("HOME", str(tmp_path))

    exit_code = profile_engine.main(
        ["grep-prompts", "tmux", "--agent", "codex", "--max-count", "1", case.flag],
    )

    assert exit_code == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == case.expected_line_count
    assert json.loads(lines[0])["profile_component"] == "grep-prompts"


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


def test_profile_main_emits_otel_root_and_render_child(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The profiler script exports a non-single root when OTel is enabled."""
    import agentgrep._telemetry as telemetry

    _ = _write_codex_session(tmp_path, name="match.jsonl", text="tmux prompt")
    monkeypatch.setenv("HOME", str(tmp_path))
    backend = telemetry.InMemoryTelemetryBackend()

    def fake_setup(**_kwargs: object) -> telemetry.TelemetryHandle:
        telemetry.configure_backend(backend)
        remove_handler = telemetry.install_logging_exporter(backend)
        return telemetry.TelemetryHandle(
            mode="test",
            backend=backend,
            _remove_logging=remove_handler,
        )

    monkeypatch.setattr(telemetry, "setup", fake_setup)

    try:
        exit_code = profile_engine.main(
            ["grep-prompts", "tmux", "--agent", "codex", "--max-count", "1", "--json"],
        )
    finally:
        telemetry.configure_backend(None)

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["profile_component"] == "grep-prompts"
    assert backend.single_root_trace_ids() == ()
    assert [span.name for span in backend.finished_spans[-3:]] == [
        "agentgrep.profile_engine.execute",
        "agentgrep.profile_engine.render",
        "agentgrep.profile_engine.run",
    ]
    root = backend.finished_spans[-1]
    assert root.attributes["agentgrep_profile_component"] == "grep-prompts"
    assert root.attributes["agentgrep_profile_component_count"] == 1
    assert root.attributes["agentgrep_command"] == "grep"
    assert root.attributes["agentgrep_scope"] == "prompts"
    assert root.attributes["agentgrep_agent_count"] == 1
    assert root.attributes["agentgrep_output_format"] == "json"
    assert root.attributes["agentgrep_result_limit"] == 1
    assert "tmux" not in str(root.attributes)
    assert str(tmp_path) not in str(root.attributes)
    profile_logs = [
        record for record in backend.log_records if record.logger_name == "agentgrep.profile_engine"
    ]
    assert [record.message for record in profile_logs] == [
        "profile engine started",
        "profile engine completed",
    ]
    assert {record.trace_id for record in backend.log_records} == {root.trace_id}
    assert all(record.span_id is not None for record in backend.log_records)


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


def test_profile_cursor_ide_run_reports_sqlite_source_spans(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor IDE profiling walks state.vscdb sources with SQLite metadata."""
    _ = _write_cursor_state_vscdb(tmp_path, text="private-cursor-token prompt")
    monkeypatch.setenv("HOME", str(tmp_path))
    parser = profile_engine._build_parser()
    args = parser.parse_args(
        [
            "search-prompts",
            "private-cursor-token",
            "--agent",
            "cursor-ide",
            "--limit",
            "1",
        ],
    )

    payload = profile_engine._run(args)

    assert payload["profile_component"] == "search-prompts"
    assert payload["profile_command"] == "search"
    assert payload["agent_count"] == 1
    assert payload["discovered_source_count"] == 1
    assert payload["planned_source_count"] == 1
    assert payload["result_count"] == 1
    profile = t.cast("dict[str, object]", payload["profile"])
    samples = t.cast("list[dict[str, object]]", profile["samples"])
    sqlite_samples = [
        sample
        for sample in samples
        if t.cast("dict[str, object]", sample["attributes"]).get("agentgrep_source_kind")
        == "sqlite"
    ]
    assert sqlite_samples
    assert {
        t.cast("dict[str, object]", sample["attributes"]).get("agentgrep_agent")
        for sample in sqlite_samples
    } == {"cursor-ide"}
    assert {
        t.cast("dict[str, object]", sample["attributes"]).get("agentgrep_store")
        for sample in sqlite_samples
    } == {"cursor-ide.state_vscdb"}
    encoded = json.dumps(payload, sort_keys=True)
    assert "private-cursor-token" not in encoded
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


class QueryLanguageBuildCase(t.NamedTuple):
    """One profiler query and whether ``--query-language`` compiles a predicate."""

    test_id: str
    component: str
    terms: tuple[str, ...]
    query_language: bool
    expects_compiled: bool


QUERY_LANGUAGE_BUILD_CASES: tuple[QueryLanguageBuildCase, ...] = (
    QueryLanguageBuildCase(
        test_id="field-predicate-compiles",
        component="search-prompts",
        terms=("agent:codex", "deploy"),
        query_language=True,
        expects_compiled=True,
    ),
    QueryLanguageBuildCase(
        test_id="boolean-compiles",
        component="grep-conversations",
        terms=("ruff", "OR", "uv"),
        query_language=True,
        expects_compiled=True,
    ),
    QueryLanguageBuildCase(
        test_id="bare-terms-stay-uncompiled",
        component="search-prompts",
        terms=("deploy", "release"),
        query_language=True,
        expects_compiled=False,
    ),
    QueryLanguageBuildCase(
        test_id="flag-off-never-compiles",
        component="search-prompts",
        terms=("agent:codex", "deploy"),
        query_language=False,
        expects_compiled=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    QUERY_LANGUAGE_BUILD_CASES,
    ids=[c.test_id for c in QUERY_LANGUAGE_BUILD_CASES],
)
def test_query_language_build_attaches_compiled(case: QueryLanguageBuildCase) -> None:
    """``--query-language`` compiles field/boolean queries into the SearchQuery."""
    parser = profile_engine._build_parser()
    argv = [case.component, *case.terms, "--agent", "all", "--limit", "5"]
    if case.query_language:
        argv.append("--query-language")
    args = parser.parse_args(argv)
    spec = profile_engine._resolve_component_specs(args)[0]
    limit = profile_engine._resolve_result_limit(args)
    query, _scope = profile_engine._build_search_query(args, spec, agents=args.agent, limit=limit)
    assert (query.compiled is not None) is case.expects_compiled


def test_query_language_rejected_for_find_profiler(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--query-language`` errors for the find profiler (no compiled query support)."""
    with pytest.raises(SystemExit) as exc_info:
        profile_engine.main(
            [
                "find-prompts",
                "agent:codex",
                "--agent",
                "all",
                "--limit",
                "1",
                "--format",
                "json",
                "--query-language",
            ],
        )

    assert exc_info.value.code == 2
    assert "query-language" in capsys.readouterr().err
