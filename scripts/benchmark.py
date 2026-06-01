#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "click>=8.1",
#     "typer>=0.12",
#     "rich>=13.0",
#     "pydantic>=2.0",
# ]
# ///
"""Cross-commit benchmark harness — versatile, project-aware, ``git bisect``-shaped.

Hyperfine across multiple commits in one invocation: HEAD, trunk, a range,
the last ``N`` commits, or an explicit list of tags / SHAs / refs. Default
benchmark definitions live in ``scripts/benchmark.toml`` (committed) and may
be overridden per-machine via ``scripts/benchmark.local.toml`` (gitignored).

Run ``uv run scripts/benchmark.py --help`` for the subcommand list, or see
``docs/dev/benchmark.md`` for invocation recipes.
"""

from __future__ import annotations

import atexit
import contextlib
import csv
import dataclasses
import io
import json
import math
import pathlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import typing as t

import click.exceptions
import pydantic
import rich.console
import rich.table
import typer

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "scripts" / "benchmark.toml"
LOCAL_CONFIG = REPO_ROOT / "scripts" / "benchmark.local.toml"

Status = t.Literal["ok", "checkout_fail", "sync_fail", "command_missing", "bench_fail"]
OutputFormat = t.Literal["rich", "json", "ndjson", "md", "csv"]
PERCENTILE_LABELS: tuple[str, ...] = ("min", "max", "avg", "p50", "p90", "p95", "p99")
SCHEMA_VERSION = 1
BENCHMARK_RUNS_ARTIFACT_KIND = "agentgrep.benchmark.runs"
BENCHMARK_MEASUREMENT_ARTIFACT_KIND = "agentgrep.benchmark.measurement"
type CommandContext = dict[str, str]
type ProfilePayload = dict[str, object]

PROFILE_ENGINE_BENCHMARK_GROUP: tuple[str, ...] = (
    "profile-engine-search-all-prompts-limit-500",
    "profile-engine-search-all-conversations-limit-500",
    "profile-engine-grep-all-prompts-max-count-500",
    "profile-engine-grep-all-conversations-max-count-500",
    "profile-engine-find-all-prompts-limit-500",
)
BENCHMARK_COMMAND_GROUPS: dict[str, tuple[str, ...]] = {
    "profile-engine": PROFILE_ENGINE_BENCHMARK_GROUP,
}


@dataclasses.dataclass(frozen=True, slots=True)
class ProfileSpanSummary:
    """One flattened profile span from a benchmark row's child profile payload."""

    short_sha: str
    command_name: str
    component: str
    name: str
    duration_seconds: float
    attributes: dict[str, object]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class BenchCommand(pydantic.BaseModel):
    """One configured benchmark — a templated shell command + metadata.

    ``command`` supports ``{venv}``, ``{query}``, ``{sha}``, ``{short_sha}``,
    ``{repo}`` placeholders. ``skip_if_missing``, if set, names a subcommand
    to probe via ``<venv>/bin/<binary> <skip_if_missing> --help`` before
    the bench runs; a non-zero exit marks the row ``command_missing``.
    """

    model_config = pydantic.ConfigDict(extra="forbid")

    description: str = ""
    command: str
    default_query: str = ""
    skip_if_missing: str | None = None


class Settings(pydantic.BaseModel):
    """Global knobs for the harness (defaults overlay each TOML layer)."""

    model_config = pydantic.ConfigDict(extra="forbid")

    warmup: int = pydantic.Field(default=1, ge=0)
    # Minimum 1 -- hyperfine with --runs 0 writes an empty JSON file that
    # explodes our parser, and the DIY fallback's empty samples list isn't
    # useful either. A user who explicitly wants "don't run the bench" can
    # use --dry-run.
    runs: int = pydantic.Field(default=3, ge=1)
    trunk: str = "master"
    sync_command: str = "uv sync --quiet"
    venv: str = ".venv"
    timeout_seconds: int = pydantic.Field(default=300, ge=1)


class Config(pydantic.BaseModel):
    """The merged config the harness operates against."""

    model_config = pydantic.ConfigDict(extra="forbid")

    bench: dict[str, BenchCommand] = pydantic.Field(default_factory=dict)
    settings: Settings = pydantic.Field(default_factory=Settings)


class Measurement(pydantic.BaseModel):
    """One (commit, bench) result — preserves raw samples for downstream stats."""

    schema_version: int = SCHEMA_VERSION
    artifact_kind: str = BENCHMARK_MEASUREMENT_ARTIFACT_KIND
    sha: str
    short_sha: str
    subject: str
    command_name: str
    command_string: str
    samples: list[float] = pydantic.Field(default_factory=list)
    status: Status = "ok"
    error: str | None = None
    dry_run: bool = False
    profile_payload: ProfilePayload | None = None
    profile_capture_error: str | None = None

    @property
    def min_s(self) -> float:
        """Fastest sample (seconds); ``nan`` for empty / failed measurements."""
        return min(self.samples) if self.samples else float("nan")

    @property
    def max_s(self) -> float:
        """Slowest sample (seconds); ``nan`` for empty / failed measurements."""
        return max(self.samples) if self.samples else float("nan")

    @property
    def avg_s(self) -> float:
        """Arithmetic mean across samples (seconds); ``nan`` when empty."""
        if not self.samples:
            return float("nan")
        return sum(self.samples) / len(self.samples)

    @property
    def stddev_s(self) -> float:
        """Sample standard deviation (Bessel-corrected); 0.0 when fewer than two samples."""
        if len(self.samples) < 2:
            return 0.0
        mean = self.avg_s
        var = sum((x - mean) ** 2 for x in self.samples) / (len(self.samples) - 1)
        return math.sqrt(var)

    def percentile(self, pct: float) -> float:
        """Nearest-rank percentile.

        Matches the convention used by the prototype bench (``/tmp/analyze-all.py``)
        so per-commit numbers carried over from earlier reports stay comparable.
        """
        if not self.samples:
            return float("nan")
        sorted_vals = sorted(self.samples)
        k = max(1, math.ceil(pct / 100.0 * len(sorted_vals)))
        return sorted_vals[k - 1]


# ---------------------------------------------------------------------------
# Stats helpers (also exposed for the test suite)
# ---------------------------------------------------------------------------


def percentile(sorted_vals: t.Sequence[float], pct: float) -> float:
    """Nearest-rank percentile on a pre-sorted sequence.

    Examples
    --------
    >>> percentile([1.0, 2.0, 3.0, 4.0], 50)
    2.0
    >>> percentile([1.0, 2.0, 3.0, 4.0], 100)
    4.0
    >>> percentile([], 50)
    nan
    """
    if not sorted_vals:
        return float("nan")
    k = max(1, math.ceil(pct / 100.0 * len(sorted_vals)))
    return sorted_vals[k - 1]


def stat_for_label(samples: t.Sequence[float], label: str) -> float:
    """Resolve a stat label (``min``/``max``/``avg``/``p50``/...) to a number."""
    if not samples:
        return float("nan")
    if label == "min":
        return min(samples)
    if label == "max":
        return max(samples)
    if label == "avg":
        return sum(samples) / len(samples)
    if label.startswith("p"):
        pct = float(label[1:])
        return percentile(sorted(samples), pct)
    msg = f"unknown stat label: {label!r}"
    raise ValueError(msg)


# ---------------------------------------------------------------------------
# Config layering
# ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, t.Any], overlay: dict[str, t.Any]) -> dict[str, t.Any]:
    """Recursively merge ``overlay`` into a copy of ``base`` (overlay wins)."""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_toml(path: pathlib.Path) -> dict[str, t.Any]:
    """Read a TOML file as a plain dict, or return ``{}`` when absent.

    Wraps :exc:`tomllib.TOMLDecodeError` as :class:`typer.BadParameter`
    so a malformed user-supplied config produces a one-line error
    instead of a stack trace from the harness's entrypoint.
    """
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        msg = f"failed to parse {path}: {exc}"
        raise typer.BadParameter(msg) from exc


def load_config(
    *,
    config_path: pathlib.Path | None = None,
    local_path: pathlib.Path | None = None,
    cli_overrides: dict[str, t.Any] | None = None,
) -> Config:
    """Load the effective config by layering defaults → TOML → local → CLI overrides.

    Layer precedence (lowest → highest):

    1. Built-in defaults (pydantic model defaults).
    2. ``scripts/benchmark.toml`` (or ``config_path`` if supplied).
    3. ``scripts/benchmark.local.toml`` (or ``local_path`` if supplied).
    4. ``cli_overrides`` dict (CLI flags).

    Pydantic validation errors (unknown keys, wrong types, missing
    required fields) are converted to :class:`typer.BadParameter` so
    they surface as one-line errors via main()'s UsageError handler.
    """
    layers: list[dict[str, t.Any]] = [{}]
    primary = config_path if config_path is not None else DEFAULT_CONFIG
    layers.append(_read_toml(primary))
    local = local_path if local_path is not None else LOCAL_CONFIG
    layers.append(_read_toml(local))
    if cli_overrides:
        layers.append(cli_overrides)
    merged: dict[str, t.Any] = {}
    for layer in layers:
        merged = _deep_merge(merged, layer)
    try:
        return Config.model_validate(merged)
    except pydantic.ValidationError as exc:
        # Surface each field that failed validation on its own line so
        # the user sees exactly which TOML key (or layered CLI key)
        # tripped the schema. pydantic's str(exc) already produces a
        # readable form; strip the trailing URL line.
        lines = [
            line
            for line in str(exc).splitlines()
            if not line.strip().startswith("For further information visit")
        ]
        msg = "invalid config:\n" + "\n".join(lines)
        raise typer.BadParameter(msg) from exc


# ---------------------------------------------------------------------------
# Templating
# ---------------------------------------------------------------------------


def render_command(template: str, context: dict[str, str]) -> str:
    """Render a templated command string via ``str.format_map``.

    Unknown placeholders raise ``KeyError`` — fail loudly rather than emit a
    nonsensical command. Curly braces in literal queries should be doubled.
    """
    return template.format_map(context)


def sanitize_command_string(command_string: str, context: CommandContext) -> str:
    """Return a shareable command string with local values replaced."""
    replacements = {
        context.get("repo", ""): "{repo}",
        context.get("venv", ""): "{venv}",
        str(pathlib.Path.home()): "{home}",
    }
    query = context.get("query", "")
    if query:
        replacements[query] = "{query}"
    sanitized = command_string
    for raw, placeholder in sorted(
        replacements.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if raw:
            sanitized = sanitized.replace(raw, placeholder)
    return sanitized


# ---------------------------------------------------------------------------
# Git target resolution
# ---------------------------------------------------------------------------


class CommitRef(t.NamedTuple):
    """A commit's identifying triple — full SHA, short SHA, subject."""

    sha: str
    short_sha: str
    subject: str


def _git(*args: str, repo: pathlib.Path = REPO_ROOT) -> str:
    """Run ``git <args>`` in ``repo`` and return stripped stdout."""
    proc = subprocess.run(
        ("git", *args),
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _resolve_sha(ref: str, *, repo: pathlib.Path = REPO_ROOT) -> str:
    """Return the full SHA for an arbitrary git ref."""
    return _git("rev-parse", ref, repo=repo)


def _subject(sha: str, *, repo: pathlib.Path = REPO_ROOT) -> str:
    """Return the commit subject for ``sha``."""
    return _git("log", "-1", "--pretty=format:%s", sha, repo=repo)


def _commit_ref(sha: str, *, repo: pathlib.Path = REPO_ROOT) -> CommitRef:
    """Build a :class:`CommitRef` for ``sha``."""
    full = _resolve_sha(sha, repo=repo)
    return CommitRef(sha=full, short_sha=full[:7], subject=_subject(full, repo=repo))


def resolve_target(
    *,
    target: str | None = None,
    range_spec: str | None = None,
    lookback: int | None = None,
    from_trunk_back: int | None = None,
    tags: bool = False,
    commits: str | None = None,
    head_vs_trunk: bool = False,
    trunk: str = "master",
    repo: pathlib.Path = REPO_ROOT,
    git_runner: t.Callable[[tuple[str, ...]], str] | None = None,
) -> list[CommitRef]:
    """Resolve a target selector to a chronologically-ordered list of commits.

    Selectors are mutually exclusive; the caller is responsible for not
    passing more than one. ``git_runner`` is an optional injection point
    for tests — it receives the git argv tuple and returns stdout text.
    """

    def run(*args: str) -> str:
        if git_runner is not None:
            return git_runner(args).strip()
        return _git(*args, repo=repo)

    def commit_for(sha: str) -> CommitRef:
        full = run("rev-parse", sha)
        subject = run("log", "-1", "--pretty=format:%s", full)
        return CommitRef(sha=full, short_sha=full[:7], subject=subject)

    if head_vs_trunk:
        return [commit_for("HEAD"), commit_for(trunk)]
    if commits:
        return [commit_for(s.strip()) for s in commits.split(",") if s.strip()]
    if tags:
        out = run("tag", "--sort=v:refname")
        names = [line for line in out.splitlines() if line.strip()]
        return [commit_for(name) for name in names]
    if range_spec:
        out = run("rev-list", "--reverse", range_spec)
        shas = [line for line in out.splitlines() if line.strip()]
        return [commit_for(sha) for sha in shas]
    if lookback is not None:
        out = run("rev-list", "-n", str(lookback), "HEAD")
        shas = list(reversed([line for line in out.splitlines() if line.strip()]))
        return [commit_for(sha) for sha in shas]
    if from_trunk_back is not None:
        out = run("rev-list", "-n", str(from_trunk_back), trunk)
        shas = list(reversed([line for line in out.splitlines() if line.strip()]))
        return [commit_for(sha) for sha in shas]
    # Default / explicit single ref
    return [commit_for(target or "HEAD")]


# ---------------------------------------------------------------------------
# Timing engine
# ---------------------------------------------------------------------------


def have_hyperfine() -> bool:
    """Return True when hyperfine is on PATH."""
    return shutil.which("hyperfine") is not None


def _time_with_hyperfine(
    cmd_str: str,
    *,
    warmup: int,
    runs: int,
    timeout_seconds: int,
) -> list[float]:
    """Run ``hyperfine -N`` and return the raw sample list (seconds)."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        export_path = pathlib.Path(tmp.name)
    try:
        subprocess.run(
            (
                "hyperfine",
                "--warmup",
                str(warmup),
                "--runs",
                str(runs),
                "-N",
                "--export-json",
                str(export_path),
                cmd_str,
            ),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        data = json.loads(export_path.read_text())
        return [float(x) for x in data["results"][0]["times"]]
    finally:
        export_path.unlink(missing_ok=True)


def _time_diy(
    cmd_str: str,
    *,
    warmup: int,
    runs: int,
    timeout_seconds: int,
) -> list[float]:
    """Pure-Python fallback timing — used when hyperfine isn't on PATH."""
    argv = shlex.split(cmd_str)
    for _ in range(warmup):
        subprocess.run(
            argv,
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
    samples: list[float] = []
    for _ in range(runs):
        t0 = time.perf_counter()
        subprocess.run(
            argv,
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
        samples.append(time.perf_counter() - t0)
    return samples


def time_command(
    cmd_str: str,
    *,
    warmup: int,
    runs: int,
    timeout_seconds: int,
    prefer_hyperfine: bool = True,
) -> list[float]:
    """Time ``cmd_str``; prefer hyperfine, fall back to pure Python."""
    if prefer_hyperfine and have_hyperfine():
        return _time_with_hyperfine(
            cmd_str,
            warmup=warmup,
            runs=runs,
            timeout_seconds=timeout_seconds,
        )
    return _time_diy(
        cmd_str,
        warmup=warmup,
        runs=runs,
        timeout_seconds=timeout_seconds,
    )


def _is_profile_engine_command(cmd_str: str) -> bool:
    """Return True when ``cmd_str`` invokes the engine profiler helper."""
    return any(token.endswith("scripts/profile_engine.py") for token in shlex.split(cmd_str))


def _capture_profile_payload(
    cmd_str: str,
    *,
    timeout_seconds: int,
) -> tuple[ProfilePayload | None, str | None]:
    """Run an engine-profiler command once and parse its sanitized JSON payload."""
    try:
        completed = subprocess.run(
            shlex.split(cmd_str),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return None, f"profile capture timed out after {timeout_seconds}s"
    if completed.returncode != 0:
        return None, f"profile capture exited {completed.returncode}"
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None, "profile capture emitted invalid JSON"
    if not isinstance(payload, dict):
        return None, "profile capture emitted non-object JSON"
    return t.cast("ProfilePayload", payload), None


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _percentile_value(m: Measurement, label: str) -> float:
    if m.status != "ok":
        return float("nan")
    return stat_for_label(m.samples, label)


def _fmt_seconds(value: float) -> str:
    if math.isnan(value):
        return "—"
    return f"{value:.3f}s"


def _profile_payload_samples(payload: ProfilePayload) -> tuple[dict[str, object], ...]:
    """Return sample dictionaries from one child profile payload."""
    profile = payload.get("profile")
    if not isinstance(profile, dict):
        return ()
    samples = profile.get("samples")
    if not isinstance(samples, list):
        return ()
    return tuple(sample for sample in samples if isinstance(sample, dict))


def _safe_profile_attributes(attributes: object) -> str:
    """Render sanitized scalar profile attributes for rich benchmark output."""
    if not isinstance(attributes, dict):
        return ""
    cells: list[str] = []
    denied_key_parts = ("argv", "command", "path", "query")
    for key, value in sorted(attributes.items()):
        if not isinstance(key, str):
            continue
        if any(part in key.casefold() for part in denied_key_parts):
            continue
        if isinstance(value, str | int | float | bool) or value is None:
            cells.append(f"{key}={value}")
        if len(cells) >= 5:
            break
    return ", ".join(cells)


def _profile_span_summaries(
    measurements: list[Measurement],
    *,
    top_spans: int,
) -> tuple[ProfileSpanSummary, ...]:
    """Return the slowest nested profile spans across benchmark measurements."""
    if top_spans <= 0:
        return ()
    summaries: list[ProfileSpanSummary] = []
    for measurement in measurements:
        payload = measurement.profile_payload
        if payload is None:
            continue
        component = payload.get("profile_component")
        component_text = component if isinstance(component, str) else "unknown"
        for sample in _profile_payload_samples(payload):
            name = sample.get("name")
            duration = sample.get("duration_seconds")
            attributes = sample.get("attributes")
            summaries.append(
                ProfileSpanSummary(
                    short_sha=measurement.short_sha,
                    command_name=measurement.command_name,
                    component=component_text,
                    name=name if isinstance(name, str) else "unknown",
                    duration_seconds=float(duration) if isinstance(duration, int | float) else 0.0,
                    attributes=dict(attributes) if isinstance(attributes, dict) else {},
                ),
            )
    return tuple(
        sorted(
            summaries,
            key=lambda summary: summary.duration_seconds,
            reverse=True,
        )[:top_spans],
    )


def render_rich(
    measurements: list[Measurement],
    percentile_labels: list[str],
    *,
    top_spans: int = 10,
) -> str:
    """Render results as a Rich table (per command, one row per commit)."""
    console = rich.console.Console(record=True, file=io.StringIO(), width=120)
    by_cmd: dict[str, list[Measurement]] = {}
    for m in measurements:
        by_cmd.setdefault(m.command_name, []).append(m)
    for cmd_name, rows in by_cmd.items():
        table = rich.table.Table(
            title=f"[bold]{cmd_name}[/bold]",
            show_lines=False,
        )
        table.add_column("sha", style="cyan")
        for label in percentile_labels:
            table.add_column(label, justify="right")
        table.add_column("status")
        table.add_column("subject", overflow="fold")
        for m in rows:
            row = [
                m.short_sha,
                *(_fmt_seconds(_percentile_value(m, label)) for label in percentile_labels),
                m.status,
                m.subject[:80],
            ]
            table.add_row(*row)
        console.print(table)

    if len(by_cmd) and any(len(rows) > 1 for rows in by_cmd.values()):
        agg = rich.table.Table(title="[bold]Distribution across commits[/bold]")
        agg.add_column("command", style="cyan")
        agg.add_column("n", justify="right")
        for label in percentile_labels:
            agg.add_column(label, justify="right")
        for cmd_name, rows in by_cmd.items():
            means = sorted(m.avg_s for m in rows if m.status == "ok")
            if not means:
                continue
            cells = [
                cmd_name,
                str(len(means)),
                *(_fmt_seconds(stat_for_label(means, label)) for label in percentile_labels),
            ]
            agg.add_row(*cells)
        console.print(agg)

    profile_spans = _profile_span_summaries(measurements, top_spans=top_spans)
    if profile_spans:
        profile_table = rich.table.Table(title="[bold]profile payload slowest spans[/bold]")
        profile_table.add_column("sha", style="cyan")
        profile_table.add_column("command")
        profile_table.add_column("component", style="cyan")
        profile_table.add_column("span")
        profile_table.add_column("duration", justify="right")
        profile_table.add_column("attributes", overflow="fold")
        for span in profile_spans:
            profile_table.add_row(
                span.short_sha,
                span.command_name,
                span.component,
                span.name,
                _fmt_seconds(span.duration_seconds),
                _safe_profile_attributes(span.attributes),
            )
        console.print(profile_table)

    return console.export_text()


def render_json(measurements: list[Measurement], _labels: list[str]) -> str:
    """Single JSON document — ``{"runs": [...]}`` — with raw samples preserved."""
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "artifact_kind": BENCHMARK_RUNS_ARTIFACT_KIND,
            "runs": [m.model_dump(mode="json") for m in measurements],
        },
        indent=2,
    )


def render_ndjson(measurements: list[Measurement], _labels: list[str]) -> str:
    """One JSON object per line — pipe-friendly for ``jq`` and friends."""
    return "\n".join(json.dumps(m.model_dump(mode="json")) for m in measurements)


def _md_escape(text: str) -> str:
    r"""Escape characters that break markdown table rendering.

    A literal ``|`` inside a cell ends the cell early — escape it as
    ``\|`` so the column count stays consistent. Backslashes are
    doubled first so the escape itself doesn't get eaten. Embedded
    newlines fold to spaces (a row must stay on one line).
    """
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def render_markdown(measurements: list[Measurement], percentile_labels: list[str]) -> str:
    """Markdown tables — mirrors the prototype ``performance.md`` shape."""
    out: list[str] = []
    by_cmd: dict[str, list[Measurement]] = {}
    for m in measurements:
        by_cmd.setdefault(m.command_name, []).append(m)
    for cmd_name, rows in by_cmd.items():
        out.append(f"## `{cmd_name}`\n")
        headers = ["sha", *percentile_labels, "status", "subject"]
        out.append("| " + " | ".join(headers) + " |")
        out.append("|" + "|".join(["---"] * len(headers)) + "|")
        for m in rows:
            cells = [
                f"`{m.short_sha}`",
                *(_fmt_seconds(_percentile_value(m, label)) for label in percentile_labels),
                m.status,
                _md_escape(m.subject[:80]),
            ]
            out.append("| " + " | ".join(cells) + " |")
        out.append("")
    return "\n".join(out)


def render_csv(measurements: list[Measurement], percentile_labels: list[str]) -> str:
    """Flat CSV — one row per measurement; raw samples joined by ``;``."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    headers = ["sha", "short_sha", "command", "status", *percentile_labels, "samples", "subject"]
    writer.writerow(headers)
    for m in measurements:
        row: list[str] = [
            m.sha,
            m.short_sha,
            m.command_name,
            m.status,
            *(f"{_percentile_value(m, label):.4f}" for label in percentile_labels),
            ";".join(f"{s:.4f}" for s in m.samples),
            m.subject,
        ]
        writer.writerow(row)
    return buffer.getvalue()


RENDERERS: dict[OutputFormat, t.Callable[[list[Measurement], list[str]], str]] = {
    "rich": render_rich,
    "json": render_json,
    "ndjson": render_ndjson,
    "md": render_markdown,
    "csv": render_csv,
}


# ---------------------------------------------------------------------------
# Per-commit isolation
# ---------------------------------------------------------------------------


def _git_dirty(repo: pathlib.Path) -> bool:
    """Return True when the worktree has uncommitted changes."""
    proc = subprocess.run(
        ("git", "diff-index", "--quiet", "HEAD", "--"),
        cwd=repo,
        check=False,
    )
    return proc.returncode != 0


def _checkout(sha: str, repo: pathlib.Path) -> None:
    subprocess.run(
        ("git", "checkout", "-q", sha),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _maybe_sync(
    settings: Settings,
    repo: pathlib.Path,
) -> subprocess.CompletedProcess[str] | None:
    """Run the configured sync command.

    Parameters
    ----------
    settings : Settings
        Harness settings; ``sync_command`` is the shell command to run.
    repo : pathlib.Path
        Repository root (cwd for the subprocess).

    Returns
    -------
    subprocess.CompletedProcess[str] or None
        ``None`` when ``settings.sync_command`` is empty (sync disabled).
        Otherwise the completed process so the caller can inspect
        ``returncode`` and decide whether to abort the commit.
    """
    if not settings.sync_command.strip():
        return None
    return subprocess.run(
        shlex.split(settings.sync_command),
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )


def _probe_subcommand(venv: pathlib.Path, subcommand: str, repo: pathlib.Path) -> bool:
    """Return True when ``<venv>/bin/agentgrep <sub> --help`` exits cleanly.

    The binary name is derived from ``<venv>/bin/<first executable>`` — for
    agentgrep that's ``agentgrep``; for a different project the TOML can
    pick a different probe target by adjusting ``command``.
    """
    binary = venv / "bin" / "agentgrep"
    if not binary.exists():
        return False
    proc = subprocess.run(
        (str(binary), subcommand, "--help"),
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def _run_one_commit(
    *,
    commit: CommitRef,
    config: Config,
    bench_names: list[str],
    query_overrides: dict[str, str],
    runs: int,
    warmup: int,
    no_sync: bool,
    dry_run: bool,
    repo: pathlib.Path,
    prefer_hyperfine: bool,
    notify: t.Callable[[str], None],
) -> list[Measurement]:
    """Checkout ``commit``, sync, time each bench. Returns one Measurement per bench."""
    results: list[Measurement] = []
    try:
        _checkout(commit.sha, repo)
    except subprocess.CalledProcessError as exc:
        notify(f"[{commit.short_sha}] checkout failed: {exc.stderr.strip()}")
        for name in bench_names:
            bench = config.bench[name]
            results.append(
                Measurement(
                    sha=commit.sha,
                    short_sha=commit.short_sha,
                    subject=commit.subject,
                    command_name=name,
                    command_string=bench.command,
                    samples=[],
                    status="checkout_fail",
                    error=exc.stderr.strip() or "checkout failed",
                    dry_run=dry_run,
                ),
            )
        return results

    if not no_sync and not dry_run:
        sync_result = _maybe_sync(config.settings, repo)
        if sync_result is not None and sync_result.returncode != 0:
            # `uv sync` (or whatever sync_command resolves to) failed —
            # the venv may be in a half-resolved state, so don't run any
            # benches against it. Mark every bench for this commit as
            # sync_fail so the user sees the failure in the row instead
            # of a misleading "ok" with stale samples.
            error = (sync_result.stderr or sync_result.stdout or "").strip() or "sync failed"
            notify(
                f"[{commit.short_sha}] sync failed (exit "
                f"{sync_result.returncode}); skipping benches.",
            )
            for name in bench_names:
                bench = config.bench[name]
                results.append(
                    Measurement(
                        sha=commit.sha,
                        short_sha=commit.short_sha,
                        subject=commit.subject,
                        command_name=name,
                        command_string=bench.command,
                        samples=[],
                        status="sync_fail",
                        error=(
                            f"`{config.settings.sync_command}` exited "
                            f"{sync_result.returncode}: {error[:300]}"
                        ),
                        dry_run=dry_run,
                    ),
                )
            return results

    venv = (repo / config.settings.venv).resolve()
    for name in bench_names:
        bench = config.bench[name]
        query = query_overrides.get(name, bench.default_query)
        context = {
            "venv": str(venv),
            "query": query,
            "sha": commit.sha,
            "short_sha": commit.short_sha,
            "repo": str(repo),
        }
        try:
            cmd_str = render_command(bench.command, context)
        except KeyError as exc:
            results.append(
                Measurement(
                    sha=commit.sha,
                    short_sha=commit.short_sha,
                    subject=commit.subject,
                    command_name=name,
                    command_string=bench.command,
                    samples=[],
                    status="bench_fail",
                    error=f"unknown template token: {exc.args[0]}",
                    dry_run=dry_run,
                ),
            )
            continue
        sanitized_cmd_str = sanitize_command_string(cmd_str, context)

        if dry_run:
            notify(f"[{commit.short_sha}] {name}: {cmd_str}  (dry-run)")
            results.append(
                Measurement(
                    sha=commit.sha,
                    short_sha=commit.short_sha,
                    subject=commit.subject,
                    command_name=name,
                    command_string=sanitized_cmd_str,
                    samples=[],
                    status="ok",
                    dry_run=True,
                ),
            )
            continue

        if bench.skip_if_missing and not _probe_subcommand(venv, bench.skip_if_missing, repo):
            results.append(
                Measurement(
                    sha=commit.sha,
                    short_sha=commit.short_sha,
                    subject=commit.subject,
                    command_name=name,
                    command_string=sanitized_cmd_str,
                    samples=[],
                    status="command_missing",
                    error=f"subcommand {bench.skip_if_missing!r} not available",
                    dry_run=dry_run,
                ),
            )
            continue

        notify(f"[{commit.short_sha}] {name}")
        try:
            samples = time_command(
                cmd_str,
                warmup=warmup,
                runs=runs,
                timeout_seconds=config.settings.timeout_seconds,
                prefer_hyperfine=prefer_hyperfine,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            results.append(
                Measurement(
                    sha=commit.sha,
                    short_sha=commit.short_sha,
                    subject=commit.subject,
                    command_name=name,
                    command_string=sanitized_cmd_str,
                    samples=[],
                    status="bench_fail",
                    error=str(exc),
                    dry_run=dry_run,
                ),
            )
            continue
        profile_payload: ProfilePayload | None = None
        profile_capture_error: str | None = None
        if _is_profile_engine_command(cmd_str):
            profile_payload, profile_capture_error = _capture_profile_payload(
                cmd_str,
                timeout_seconds=config.settings.timeout_seconds,
            )
        results.append(
            Measurement(
                sha=commit.sha,
                short_sha=commit.short_sha,
                subject=commit.subject,
                command_name=name,
                command_string=sanitized_cmd_str,
                samples=samples,
                status="ok",
                dry_run=dry_run,
                profile_payload=profile_payload,
                profile_capture_error=profile_capture_error,
            ),
        )
    return results


# ---------------------------------------------------------------------------
# HEAD restore guard
# ---------------------------------------------------------------------------


def _install_restore_guard(
    *,
    repo: pathlib.Path,
    original_ref: str,
    keep_checkout: bool,
) -> None:
    """Register an atexit + SIGINT/SIGTERM trap that restores ``original_ref``."""
    if keep_checkout:
        return
    restored = {"done": False}

    def _restore() -> None:
        if restored["done"]:
            return
        restored["done"] = True
        with contextlib.suppress(OSError):
            subprocess.run(
                ("git", "checkout", "-q", original_ref),
                cwd=repo,
                check=False,
                capture_output=True,
            )

    atexit.register(_restore)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda _signo, _frame: (_restore(), sys.exit(130)))
        except ValueError, OSError:
            # Not always installable (e.g. non-main thread); fall back to atexit.
            continue


# ---------------------------------------------------------------------------
# Typer surface
# ---------------------------------------------------------------------------


app = typer.Typer(
    name="benchmark",
    help="Cross-commit benchmark harness — hyperfine across HEAD, trunk, ranges, tags.",
    add_completion=False,
    no_args_is_help=True,
)


def _parse_percentile_labels(raw: str) -> list[str]:
    labels = [piece.strip() for piece in raw.split(",") if piece.strip()]
    for label in labels:
        if label in ("min", "max", "avg"):
            continue
        if label.startswith("p") and label[1:].isdigit():
            continue
        msg = (
            f"unknown stat label: {label!r}; expected one of min/max/avg/p<N> (e.g. p50, p90, p95)"
        )
        raise typer.BadParameter(msg)
    return labels


def _select_bench_names(config: Config, commands: str | None) -> list[str]:
    if commands is None:
        return list(config.bench)
    names: list[str] = []
    for selector in (c.strip() for c in commands.split(",") if c.strip()):
        group = BENCHMARK_COMMAND_GROUPS.get(selector)
        if group is None:
            names.append(selector)
        else:
            names.extend(group)
    if not names:
        msg = "--commands did not select any benchmarks; pass a benchmark name or command group"
        raise typer.BadParameter(msg)
    missing = [n for n in names if n not in config.bench]
    if missing:
        msg = (
            f"unknown benchmark name(s): {', '.join(missing)}; available: {', '.join(config.bench)}"
        )
        raise typer.BadParameter(msg)
    return names


def _available_command_groups(config: Config) -> dict[str, tuple[str, ...]]:
    """Return command groups whose member benchmarks all exist in ``config``."""
    return {
        group_name: group_names
        for group_name, group_names in BENCHMARK_COMMAND_GROUPS.items()
        if all(bench_name in config.bench for bench_name in group_names)
    }


def _select_targets(
    *,
    target: str | None,
    range_spec: str | None,
    lookback: int | None,
    from_trunk_back: int | None,
    tags: bool,
    commits: str | None,
    head_vs_trunk: bool,
    trunk: str,
) -> list[CommitRef]:
    chosen = [
        bool(range_spec),
        lookback is not None,
        from_trunk_back is not None,
        tags,
        bool(commits),
        head_vs_trunk,
        target is not None and target not in (None, "HEAD"),
    ]
    if sum(1 for c in chosen if c) > 1:
        msg = "Target selectors are mutually exclusive — pass only one of --target/--range/--lookback/--from-trunk-back/--tags/--commits/--head-vs-trunk."
        raise typer.BadParameter(msg)
    if target == "trunk":
        target = trunk
    try:
        return resolve_target(
            target=target,
            range_spec=range_spec,
            lookback=lookback,
            from_trunk_back=from_trunk_back,
            tags=tags,
            commits=commits,
            head_vs_trunk=head_vs_trunk,
            trunk=trunk,
        )
    except subprocess.CalledProcessError as exc:
        # git rejected one of the supplied refs (bad SHA, unknown branch,
        # malformed range). Convert to a clean BadParameter so the caller
        # sees a one-line "error: ..." instead of a Python traceback.
        stderr = (exc.stderr or "").strip() if isinstance(exc.stderr, str) else ""
        msg = f"git failed resolving target: {' '.join(exc.cmd)}"
        if stderr:
            msg = f"{msg}\n{stderr}"
        raise typer.BadParameter(msg) from exc


# Shared flag defaults
_OPT_TARGET = typer.Option("HEAD", "--target", help="Single ref: HEAD / trunk / tag / SHA.")
_OPT_RANGE = typer.Option(None, "--range", help="git range, e.g. master..HEAD.")
_OPT_LOOKBACK = typer.Option(None, "--lookback", help="Last N commits from HEAD.")
_OPT_FROM_TRUNK = typer.Option(None, "--from-trunk-back", help="Trunk + N prior commits.")
_OPT_TAGS = typer.Option(False, "--tags", help="All git tags (sorted by v:refname).")
_OPT_COMMITS = typer.Option(None, "--commits", help="Comma-separated list of refs.")
_OPT_HEAD_VS_TRUNK = typer.Option(False, "--head-vs-trunk", help="Shortcut: HEAD + trunk.")
_OPT_CONFIG = typer.Option(None, "--config", help="Override scripts/benchmark.toml path.")
_OPT_COMMANDS = typer.Option(
    None,
    "--commands",
    help="Subset selector matching [bench.X] keys or command groups (comma-separated).",
)


@app.command("run")
def cmd_run(
    target: str = _OPT_TARGET,
    range_spec: str | None = _OPT_RANGE,
    lookback: int | None = _OPT_LOOKBACK,
    from_trunk_back: int | None = _OPT_FROM_TRUNK,
    tags: bool = _OPT_TAGS,
    commits: str | None = _OPT_COMMITS,
    head_vs_trunk: bool = _OPT_HEAD_VS_TRUNK,
    config_path: pathlib.Path | None = _OPT_CONFIG,
    commands: str | None = _OPT_COMMANDS,
    runs: int | None = typer.Option(None, "--runs", help="Override sample count."),
    warmup: int | None = typer.Option(None, "--warmup", help="Override discarded pre-runs."),
    query: str | None = typer.Option(
        None, "--query", help="Override the {query} template var for all benches."
    ),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip 'uv sync' between checkouts."),
    keep_checkout: bool = typer.Option(
        False, "--keep-checkout", help="Don't restore HEAD on exit."
    ),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", help="Run with uncommitted changes."),
    output_format: OutputFormat = typer.Option("rich", "--format", help="Output renderer."),
    output: pathlib.Path | None = typer.Option(
        None, "--output", help="Write rendered output to file."
    ),
    show_percentiles: str = typer.Option(
        "min,avg,p50,p90,p95,p99,max",
        "--show-percentiles",
        help="Comma-separated subset of stat labels to display.",
    ),
    top_spans: int = typer.Option(
        10,
        "--top-spans",
        min=0,
        help="Slowest nested profile_payload spans to show in rich output; 0 disables.",
    ),
    no_progress: bool = typer.Option(
        False, "--no-progress", help="Suppress progress notes on stderr."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print resolved commands without running."
    ),
    no_hyperfine: bool = typer.Option(
        False, "--no-hyperfine", help="Force the pure-Python timing fallback."
    ),
) -> None:
    """Run the configured benchmarks across the targeted commits."""
    # CLI overrides go through load_config so pydantic validators
    # (e.g. settings.runs >= 1) fire on bad input. model_copy(update=...)
    # would silently skip the validation gate.
    cli_overrides: dict[str, t.Any] = {}
    settings_override: dict[str, t.Any] = {}
    if runs is not None:
        settings_override["runs"] = runs
    if warmup is not None:
        settings_override["warmup"] = warmup
    if settings_override:
        cli_overrides["settings"] = settings_override
    config = load_config(
        config_path=config_path,
        cli_overrides=cli_overrides or None,
    )

    if not config.bench:
        typer.echo("error: no [bench.*] entries in config — nothing to benchmark.", err=True)
        raise typer.Exit(code=2)

    bench_names = _select_bench_names(config, commands)
    percentile_labels = _parse_percentile_labels(show_percentiles)
    query_overrides = dict.fromkeys(bench_names, query) if query is not None else {}

    if output is not None:
        # Pre-flight: fail fast before benchmarking. Otherwise a typo in
        # --output (or a directory target) only surfaces after minutes
        # of bench runs, and the user has nothing to show for it.
        if output.is_dir():
            typer.echo(f"error: --output points at a directory: {output}", err=True)
            raise typer.Exit(code=2)
        if not output.parent.exists():
            typer.echo(
                f"error: --output parent does not exist: {output.parent}",
                err=True,
            )
            raise typer.Exit(code=2)

    repo = REPO_ROOT
    if not allow_dirty and _git_dirty(repo):
        typer.echo(
            "error: worktree has uncommitted changes; commit/stash first or pass --allow-dirty.",
            err=True,
        )
        raise typer.Exit(code=2)

    targets = _select_targets(
        target=target,
        range_spec=range_spec,
        lookback=lookback,
        from_trunk_back=from_trunk_back,
        tags=tags,
        commits=commits,
        head_vs_trunk=head_vs_trunk,
        trunk=config.settings.trunk,
    )

    original_ref = _git("rev-parse", "--abbrev-ref", "HEAD", repo=repo)
    if original_ref == "HEAD":
        original_ref = _git("rev-parse", "HEAD", repo=repo)
    _install_restore_guard(repo=repo, original_ref=original_ref, keep_checkout=keep_checkout)

    def notify(line: str) -> None:
        if no_progress:
            return
        typer.echo(line, err=True)

    measurements: list[Measurement] = []
    for i, commit in enumerate(targets, start=1):
        notify(f"[{i}/{len(targets)}] {commit.short_sha} {commit.subject[:60]}")
        measurements.extend(
            _run_one_commit(
                commit=commit,
                config=config,
                bench_names=bench_names,
                query_overrides=query_overrides,
                runs=config.settings.runs,
                warmup=config.settings.warmup,
                no_sync=no_sync,
                dry_run=dry_run,
                repo=repo,
                prefer_hyperfine=not no_hyperfine,
                notify=notify,
            ),
        )

    if output_format == "rich":
        rendered = render_rich(measurements, percentile_labels, top_spans=top_spans)
    else:
        rendered = RENDERERS[output_format](measurements, percentile_labels)
    if output is not None:
        output.write_text(rendered)
        notify(f"wrote {output}")
    else:
        typer.echo(rendered)


@app.command("compare")
def cmd_compare(
    a: str = typer.Argument(..., help="First ref (tag / branch / SHA)."),
    b: str = typer.Argument(..., help="Second ref (tag / branch / SHA)."),
    config_path: pathlib.Path | None = _OPT_CONFIG,
    commands: str | None = _OPT_COMMANDS,
    runs: int | None = typer.Option(None, "--runs"),
    warmup: int | None = typer.Option(None, "--warmup"),
    query: str | None = typer.Option(None, "--query"),
    output_format: OutputFormat = typer.Option("rich", "--format"),
    output: pathlib.Path | None = typer.Option(None, "--output"),
    show_percentiles: str = typer.Option("min,avg,p50,p90,p95,p99,max", "--show-percentiles"),
    top_spans: int = typer.Option(10, "--top-spans", min=0),
    no_sync: bool = typer.Option(False, "--no-sync"),
    keep_checkout: bool = typer.Option(False, "--keep-checkout"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty"),
    no_progress: bool = typer.Option(False, "--no-progress"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    no_hyperfine: bool = typer.Option(False, "--no-hyperfine"),
) -> None:
    """Run benchmarks against two commits — sugar for ``run --commits A,B``."""
    cmd_run(
        target=None,
        range_spec=None,
        lookback=None,
        from_trunk_back=None,
        tags=False,
        commits=f"{a},{b}",
        head_vs_trunk=False,
        config_path=config_path,
        commands=commands,
        runs=runs,
        warmup=warmup,
        query=query,
        no_sync=no_sync,
        keep_checkout=keep_checkout,
        allow_dirty=allow_dirty,
        output_format=output_format,
        output=output,
        show_percentiles=show_percentiles,
        top_spans=top_spans,
        no_progress=no_progress,
        dry_run=dry_run,
        no_hyperfine=no_hyperfine,
    )


@app.command("list-commits")
def cmd_list_commits(
    target: str = _OPT_TARGET,
    range_spec: str | None = _OPT_RANGE,
    lookback: int | None = _OPT_LOOKBACK,
    from_trunk_back: int | None = _OPT_FROM_TRUNK,
    tags: bool = _OPT_TAGS,
    commits: str | None = _OPT_COMMITS,
    head_vs_trunk: bool = _OPT_HEAD_VS_TRUNK,
    config_path: pathlib.Path | None = _OPT_CONFIG,
) -> None:
    """Print the commits a target spec would resolve to (no benchmarks run)."""
    config = load_config(config_path=config_path)
    targets = _select_targets(
        target=target,
        range_spec=range_spec,
        lookback=lookback,
        from_trunk_back=from_trunk_back,
        tags=tags,
        commits=commits,
        head_vs_trunk=head_vs_trunk,
        trunk=config.settings.trunk,
    )
    for c in targets:
        typer.echo(f"{c.short_sha}  {c.subject}")


@app.command("list-commands")
def cmd_list_commands(config_path: pathlib.Path | None = _OPT_CONFIG) -> None:
    """Print the configured benchmark commands (post-layering)."""
    config = load_config(config_path=config_path)
    for name, bench in config.bench.items():
        typer.echo(f"{name}:")
        if bench.description:
            typer.echo(f"  description: {bench.description}")
        typer.echo(f"  command:     {bench.command}")
        if bench.default_query:
            typer.echo(f"  query:       {bench.default_query}")
        if bench.skip_if_missing:
            typer.echo(f"  skip-probe:  {bench.skip_if_missing}")
    groups = _available_command_groups(config)
    if groups:
        typer.echo("command groups:")
        for name, members in groups.items():
            typer.echo(f"  {name}: {', '.join(members)}")


@app.command("show-config")
def cmd_show_config(config_path: pathlib.Path | None = _OPT_CONFIG) -> None:
    """Dump the effective config (post-layering) as JSON."""
    config = load_config(config_path=config_path)
    typer.echo(json.dumps(config.model_dump(mode="json"), indent=2))


def main(argv: list[str] | None = None) -> int:
    """Entry point — dispatches to the typer app.

    ``standalone_mode=False`` keeps Click from calling ``sys.exit`` itself.
    In that mode Click *converts* :class:`typer.Exit` into the return value
    of ``app(...)`` instead of re-raising it — so commands that
    ``raise typer.Exit(code=2)`` deliver that 2 via the ``result`` below,
    not via an except clause. Click and Typer validation exceptions still
    propagate as exceptions, so catch both families and print a one-line
    error instead of a rich traceback.
    """
    try:
        result = app(args=argv, standalone_mode=False)
    except click.exceptions.UsageError as exc:
        typer.echo(f"error: {exc.format_message()}", err=True)
        return 2
    except typer.BadParameter as exc:
        typer.echo(f"error: {exc}", err=True)
        return 2
    if isinstance(result, int):
        return result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
