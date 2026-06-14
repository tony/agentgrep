"""Rendering and dispatch for the ``agentgrep insights`` command tree.

Every ``run_insights_*`` dispatcher imports :mod:`agentgrep.insights`
function-locally so ``import agentgrep`` (and the root ``--help`` path)
never pays for the insights package or any optional backend. The console
progress sink streams phase lines, download bytes, and live LLM tokens to
stderr so long-running enrichment is visible without polluting stdout.
"""

from __future__ import annotations

import json
import sys
import typing as t

if t.TYPE_CHECKING:
    from agentgrep.cli.parser import (
        InsightsCacheArgs,
        InsightsDoctorArgs,
        InsightsLevelsArgs,
        InsightsModelsArgs,
        InsightsReportArgs,
        InsightsSetupArgs,
    )
    from agentgrep.insights.model import InsightsLevelStatus, InsightsReport

__all__ = [
    "ConsoleInsightsProgress",
    "run_insights_cache_command",
    "run_insights_doctor_command",
    "run_insights_levels_command",
    "run_insights_models_command",
    "run_insights_report_command",
    "run_insights_setup_command",
]


class ConsoleInsightsProgress:
    """A progress sink that streams phases, downloads, and LLM tokens to stderr."""

    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self._streaming = False

    def _end_stream(self) -> None:
        if self._streaming:
            sys.stderr.write("\n")
            self._streaming = False

    def phase(self, name: str, *, detail: str = "") -> None:
        """Print a phase header line."""
        if not self.enabled:
            return
        self._end_stream()
        suffix = f": {detail}" if detail else ""
        sys.stderr.write(f"[insights] {name}{suffix}\n")
        sys.stderr.flush()

    def download_progress(
        self,
        *,
        model: str,
        downloaded_bytes: int,
        total_bytes: int | None,
    ) -> None:
        """Print a single-line, in-place download progress indicator."""
        if not self.enabled:
            return
        from agentgrep.insights.cache import human_size

        self._end_stream()
        got = human_size(downloaded_bytes)
        if total_bytes:
            pct = int(downloaded_bytes / total_bytes * 100)
            sys.stderr.write(
                f"\r[insights] downloading {model} {got}/{human_size(total_bytes)} ({pct}%)"
            )
        else:
            sys.stderr.write(f"\r[insights] downloading {model} {got}")
        sys.stderr.flush()

    def llm_chunk(self, *, backend: str, model: str, delta: str, char_count: int) -> None:
        """Stream LLM token deltas to stderr as they arrive."""
        if not self.enabled:
            return
        if not self._streaming:
            sys.stderr.write(f"[insights] {backend}:{model} → ")
            self._streaming = True
        sys.stderr.write(delta)
        sys.stderr.flush()


def _make_progress(*, human: bool, progress_mode: str) -> ConsoleInsightsProgress | None:
    """Return a console progress sink when progress should be shown."""
    enabled = progress_mode == "always" or (
        progress_mode == "auto" and human and bool(getattr(sys.stderr, "isatty", lambda: False)())
    )
    if not enabled:
        return None
    return ConsoleInsightsProgress(enabled=True)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _render_report_text(report: InsightsReport) -> str:
    """Render a report as plain text for a terminal."""
    out: list[str] = [
        report.activity.summary,
        f"level: {report.level}   status: {report.status}   "
        f"records: {report.records_analyzed}" + ("  (sampled)" if report.sampled else ""),
    ]
    if report.agents:
        out.append("agents: " + ", ".join(f"{k} ({v})" for k, v in report.agents.items()))
    if report.earliest_timestamp:
        out.append(f"range: {report.earliest_timestamp} → {report.latest_timestamp}")
    if report.top_terms:
        out.append("")
        out.append(
            "Top terms: " + ", ".join(f"{term.term} ({term.count})" for term in report.top_terms)
        )

    if report.activity.work_areas:
        out.append("")
        out.append("Work areas:")
        for area in report.activity.work_areas:
            terms = ", ".join(term.term for term in area.top_terms)
            out.append(f"  - {area.label}  ({area.record_count} records)  {terms}")

    if report.activity.timeline:
        out.append("")
        out.append("Timeline:")
        out.extend(f"  {bucket.date}  {bucket.record_count}" for bucket in report.activity.timeline)

    if report.activity.repeated_instructions:
        out.append("")
        out.append("Repeated instructions:")
        out.extend(f"  - {line}" for line in report.activity.repeated_instructions)

    if report.activity.open_threads:
        out.append("")
        out.append("Open threads:")
        out.extend(
            f"  - [{thread.agent}] {thread.title}" for thread in report.activity.open_threads
        )

    for enrichment in report.enrichments:
        out.append("")
        out.append(f"Enrichment: {enrichment.level} ({enrichment.backend}) — {enrichment.status}")
        out.append(f"  {enrichment.message}")
        out.extend(_render_enrichment_detail(enrichment))

    if report.diagnostics:
        out.append("")
        out.append("Diagnostics:")
        for diag in report.diagnostics:
            cmd = f"  → {diag.setup_command}" if diag.setup_command else ""
            out.append(f"  [{diag.severity}] {diag.message}{cmd}")

    if report.next_actions:
        out.append("")
        out.append("Next:")
        out.extend(f"  $ {action}" for action in report.next_actions)
    return "\n".join(out)


def _render_enrichment_detail(enrichment: t.Any) -> list[str]:
    """Render level-specific enrichment data as indented text lines."""
    data = enrichment.data
    lines: list[str] = []
    if enrichment.level == "ml":
        lines.extend(
            f"    topic {topic['topic']} ({topic['size']}): {', '.join(topic['terms'][:6])}"
            for topic in data.get("topics", [])[:6]
        )
    elif enrichment.level == "embeddings":
        for group in data.get("semantic_groups", [])[:6]:
            snippet = (group.get("example") or {}).get("snippet") or ""
            lines.append(f"    group of {group['size']}: {snippet[:70]}")
        if data.get("duplicates"):
            lines.append(f"    near-duplicates: {len(data['duplicates'])}")
    elif enrichment.level == "index":
        lines.append(
            f"    documents: {data.get('documents_indexed')}  "
            f"vectors: {data.get('vectors_included')}  path: {data.get('index_path')}"
        )
        lines.extend(
            f"    hit: {(hit.get('snippet') or '')[:70]}" for hit in data.get("hits", [])[:5]
        )
    elif enrichment.level == "llm" and data.get("summary"):
        lines.append("")
        lines.extend(f"    {para}" for para in str(data["summary"]).splitlines())
    return lines


def _render_report_markdown(report: InsightsReport) -> str:
    """Render a report as Markdown."""
    out: list[str] = [
        "# agentgrep insights",
        "",
        f"{report.activity.summary}",
        "",
        f"- **level**: `{report.level}`",
        f"- **status**: `{report.status}`",
        f"- **records**: {report.records_analyzed}",
    ]
    if report.top_terms:
        out.append("")
        out.append("## Top terms")
        out.append(", ".join(f"`{term.term}` ({term.count})" for term in report.top_terms))
    if report.activity.work_areas:
        out.append("")
        out.append("## Work areas")
        out.append("| Area | Records | Top terms |")
        out.append("| --- | --- | --- |")
        for area in report.activity.work_areas:
            terms = ", ".join(term.term for term in area.top_terms)
            out.append(f"| {area.label} | {area.record_count} | {terms} |")
    if report.activity.open_threads:
        out.append("")
        out.append("## Open threads")
        out.extend(f"- _{thread.agent}_: {thread.title}" for thread in report.activity.open_threads)
    for enrichment in report.enrichments:
        out.append("")
        out.append(f"## Enrichment — {enrichment.level} ({enrichment.backend})")
        out.append(enrichment.message)
        if enrichment.level == "llm" and enrichment.data.get("summary"):
            out.append("")
            out.append(str(enrichment.data["summary"]))
    return "\n".join(out)


def _render_report_html(report: InsightsReport) -> str:
    """Return HTML for a report (reusing the L1 enrichment when present)."""
    for enrichment in report.enrichments:
        if enrichment.level == "html" and enrichment.data.get("html"):
            return str(enrichment.data["html"])
    import html as html_mod

    body = html_mod.escape(_render_report_text(report))
    return f"<!doctype html><html><body><pre>{body}</pre></body></html>"


def _emit_report(report: InsightsReport, output_format: str) -> None:
    """Write the report to stdout in the requested format."""
    if output_format == "json":
        print(json.dumps(report.to_payload(), ensure_ascii=False, indent=2))
    elif output_format == "ndjson":
        print(json.dumps({"type": "report.started", "scope": report.scope}, ensure_ascii=False))
        for status in report.levels:
            print(json.dumps({"type": "level", **status.to_payload()}, ensure_ascii=False))
        print(
            json.dumps(
                {"type": "report.finished", "report": report.to_payload()}, ensure_ascii=False
            )
        )
    elif output_format == "markdown":
        print(_render_report_markdown(report))
    elif output_format == "html":
        print(_render_report_html(report))
    else:
        print(_render_report_text(report))


def run_insights_report_command(args: InsightsReportArgs) -> int:
    """Collect records, build the report, and render it."""
    import pathlib

    import agentgrep
    from agentgrep import insights
    from agentgrep.insights.model import ReportRequest

    human = args.output_format in ("text", "markdown", "html")
    progress = _make_progress(human=human, progress_mode=args.progress_mode)

    interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())
    allow_download = args.allow_download and (args.yes or interactive)
    if args.allow_download and not allow_download:
        sys.stderr.write(
            "[insights] refusing to download without --yes in a non-interactive shell\n"
        )

    query = agentgrep.SearchQuery(
        terms=(),
        scope=args.scope,
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=args.agents,
        limit=args.limit,
        dedupe=True,
    )
    if progress is not None:
        progress.phase("collect", detail=f"scope={args.scope}")
    records = agentgrep.run_search_query(pathlib.Path.home(), query)

    request = ReportRequest(
        scope=args.scope,
        requested_level=args.requested_level,
        record_limit=args.limit,
        model=args.model,
        llm_backend=args.llm_backend,
        index_backend=args.index_backend,
        allow_download=allow_download,
        include_text=args.include_text,
    )
    report = insights.build_report(records, request, progress=progress)
    if progress is not None:
        progress.phase("render", detail=args.output_format)
    _emit_report(report, args.output_format)
    return 0 if report.records_analyzed > 0 else 1


# ---------------------------------------------------------------------------
# levels / doctor / setup
# ---------------------------------------------------------------------------


def _level_rows(levels: t.Sequence[InsightsLevelStatus]) -> str:
    """Render a level-availability table as text."""
    out: list[str] = ["Insights levels:"]
    for status in levels:
        mark = "✓" if status.available else "·"
        backend = status.backend or "—"
        detail = f"   {status.setup_command}" if status.setup_command else ""
        out.append(f"  {mark} {status.level:<12} {backend:<22} {status.reason}{detail}")
    return "\n".join(out)


def run_insights_levels_command(args: InsightsLevelsArgs) -> int:
    """List enrichment levels and their availability."""
    from agentgrep.insights import probe_levels
    from agentgrep.insights.model import ReportRequest

    levels = probe_levels(ReportRequest())
    if args.output_format in ("json", "ndjson"):
        if args.output_format == "json":
            print(json.dumps([s.to_payload() for s in levels], ensure_ascii=False, indent=2))
        else:
            for status in levels:
                print(json.dumps(status.to_payload(), ensure_ascii=False))
        return 0
    print(_level_rows(levels))
    return 0


def run_insights_doctor_command(args: InsightsDoctorArgs) -> int:
    """Diagnose dependency availability and cache state."""
    import platform

    from agentgrep.insights import cache as cache_mod, probe_levels
    from agentgrep.insights.cache import human_size
    from agentgrep.insights.model import ReportRequest

    levels = probe_levels(ReportRequest())
    cache_root = cache_mod.cache_dir()
    model_root = cache_mod.model_cache_dir()
    payload = {
        "python": platform.python_version(),
        "platform": sys.platform,
        "cache_dir": str(cache_root),
        "model_dir": str(model_root),
        "cache_bytes": cache_mod.directory_size_bytes(cache_root),
        "model_bytes": cache_mod.directory_size_bytes(model_root),
        "levels": [s.to_payload() for s in levels],
    }
    if args.output_format in ("json", "ndjson"):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    lines = [
        "agentgrep insights doctor",
        f"  python:    {payload['python']} ({payload['platform']})",
        f"  cache dir: {cache_root} ({human_size(payload['cache_bytes'])})",
        f"  model dir: {model_root} ({human_size(payload['model_bytes'])})",
        "",
        _level_rows(levels),
    ]
    print("\n".join(lines))
    return 0


def run_insights_setup_command(args: InsightsSetupArgs) -> int:
    """Print the install command for a level (does not run pip)."""
    from agentgrep.insights import probe_levels
    from agentgrep.insights.model import ReportRequest

    levels = {s.level: s for s in probe_levels(ReportRequest())}
    status = levels.get(t.cast("t.Any", args.level))
    if status is None:
        print(f"unknown level: {args.level}")
        return 1
    if status.available:
        print(f"level {args.level!r} is already available (backend: {status.backend})")
        return 0
    print(f"To enable the {args.level!r} level, run:")
    print(f"  $ {status.setup_command}")
    return 0


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


def run_insights_models_command(args: InsightsModelsArgs) -> int:
    """List or install curated models."""
    from agentgrep.insights import models as models_mod

    if args.action in ("available", "list"):
        return _run_models_listing(args, models_mod)
    return _run_models_install(args, models_mod)


def _model_row(spec: t.Any, installed: bool) -> dict[str, t.Any]:
    """Return a JSON-friendly row describing a curated model."""
    return {
        "model_id": spec.model_id,
        "kind": spec.kind,
        "backend": spec.backend,
        "license": spec.license,
        "installed": installed,
        "local_id": spec.local_id,
    }


def _run_models_listing(args: InsightsModelsArgs, models_mod: t.Any) -> int:
    """Render the curated/installed model listing."""
    if args.kind == "llm":
        specs = models_mod.list_llm_models(args.llm_backend)
    else:
        specs = models_mod.list_embedding_models()
    rows = [(spec, models_mod.is_installed(spec)) for spec in specs]
    if args.action == "list":
        rows = [(spec, installed) for spec, installed in rows if installed]

    if args.output_format in ("json", "ndjson"):
        payload = [_model_row(spec, installed) for spec, installed in rows]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("No models installed." if args.action == "list" else "No curated models.")
        return 0
    print(f"Models ({args.kind}):")
    for spec, installed in rows:
        mark = "✓" if installed else "·"
        print(f"  {mark} {spec.model_id:<22} {spec.backend:<22} {spec.license:<14} {spec.notes}")
    return 0


def _run_models_install(args: InsightsModelsArgs, models_mod: t.Any) -> int:
    """Provision one curated model."""
    if args.model is None:
        print("install requires a model id")
        return 1
    if args.kind == "llm":
        spec = models_mod.resolve_llm_model(args.model, args.llm_backend)
    else:
        spec = models_mod.resolve_embedding_model(args.model)
    if spec is None:
        print(f"unknown model {args.model!r}; see `agentgrep insights models available`")
        return 1

    interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())
    if not args.dry_run and not args.yes and not interactive:
        print("refusing to download without --yes in a non-interactive shell")
        return 1

    human = args.output_format in ("text", "markdown", "html")
    progress = _make_progress(human=human, progress_mode="auto")
    try:
        result = models_mod.install_model(spec, progress=progress, dry_run=args.dry_run)
    except Exception as exc:
        setup = getattr(exc, "setup_command", None)
        print(f"install failed: {exc}")
        if setup:
            print(f"  → {setup}")
        return 1

    if args.output_format in ("json", "ndjson"):
        print(
            json.dumps(
                {
                    "model_id": result.model_id,
                    "path": str(result.path),
                    "cached": result.cached,
                    "bytes": result.bytes_downloaded,
                    "files": list(result.files),
                    "dry_run": result.dry_run,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if result.dry_run:
        print(f"[dry-run] would download {result.model_id} → {result.path}")
        print(f"          files: {', '.join(result.files) or '(snapshot)'}")
    elif result.cached:
        print(f"{result.model_id} already installed at {result.path}")
    else:
        from agentgrep.insights.cache import human_size

        print(
            f"installed {result.model_id} → {result.path} ({human_size(result.bytes_downloaded)})"
        )
    return 0


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


def run_insights_cache_command(args: InsightsCacheArgs) -> int:
    """Inspect or prune the insights cache."""
    from agentgrep.insights import cache as cache_mod
    from agentgrep.insights.cache import human_size

    if args.action == "dir":
        payload = {
            "cache_dir": str(cache_mod.cache_dir()),
            "model_dir": str(cache_mod.model_cache_dir()),
            "index_dir": str(cache_mod.index_cache_dir()),
        }
        if args.output_format in ("json", "ndjson"):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for key, value in payload.items():
                print(f"{key}: {value}")
        return 0

    if args.action == "size":
        cache_bytes = cache_mod.directory_size_bytes(cache_mod.cache_dir())
        model_bytes = cache_mod.directory_size_bytes(cache_mod.model_cache_dir())
        if args.output_format in ("json", "ndjson"):
            print(
                json.dumps(
                    {"cache_bytes": cache_bytes, "model_bytes": model_bytes}, ensure_ascii=False
                )
            )
        else:
            print(f"cache: {human_size(cache_bytes)}")
            print(f"models: {human_size(model_bytes)}")
        return 0

    # prune
    result = cache_mod.prune_cache(dry_run=args.dry_run)
    verb = "would reclaim" if args.dry_run else "reclaimed"
    if args.output_format in ("json", "ndjson"):
        print(
            json.dumps(
                {
                    "dry_run": args.dry_run,
                    "removed": [str(path) for path in result.removed_paths],
                    "reclaimed_bytes": result.reclaimed_bytes,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(
            f"{verb} {human_size(result.reclaimed_bytes)} from {len(result.removed_paths)} path(s)"
        )
        for path in result.removed_paths:
            print(f"  - {path}")
    return 0
