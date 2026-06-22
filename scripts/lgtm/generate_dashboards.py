#!/usr/bin/env python
"""Generate the agentgrep Grafana dashboard suite as provisioned JSON.

agentgrep exports its OpenTelemetry signals to the local otel-lgtm stack
(see ``scripts/lgtm/up.sh``). This script renders a coherent set of
Grafana dashboards from the metric and label surface the app actually
emits, so the boards stay grounded in real series instead of guesswork.

The output is a folder of dashboard JSON files plus nothing else; the
provider config (``grafana-dashboards-agentgrep.yaml``) and the bind
mounts live in ``up.sh``. ``up.sh`` re-runs this generator on every
startup, mirroring ``generate_pyroscope_source_map.py``, so a fresh
checkout always has the boards available.

Design
------
The generic span instruments are the RED backbone:

- ``agentgrep_span_count_total`` — one increment per finished span,
  labelled by ``operation`` (span name), ``outcome`` (ok/error), and the
  forwarded ``agentgrep_*`` classifiers.
- ``agentgrep_span_duration_seconds`` — the matching latency histogram.

Domain histograms (``search_sources``, ``find_results``, ``grep_duration``,
…) add the "how much work" dimension that latency alone cannot show.
Latency panels enable exemplars so the metric→trace pivot is one click
away in Grafana.

Examples
--------
Render every dashboard into the default folder::

    >>> import pathlib, tempfile
    >>> out = pathlib.Path(tempfile.mkdtemp())
    >>> paths = write_dashboards(out)
    >>> sorted(p.name for p in paths)
    ['agentgrep-agentic.json', 'agentgrep-find-grep.json', 'agentgrep-mcp.json', 'agentgrep-overview.json', 'agentgrep-search.json', 'agentgrep-surfaces.json']

Each file is valid dashboard JSON with a stable ``uid``::

    >>> import json
    >>> doc = json.loads((out / "agentgrep-overview.json").read_text())
    >>> doc["uid"], doc["panels"][0]["type"]
    ('agentgrep-overview', 'row')
"""

from __future__ import annotations

import argparse
import json
import pathlib
import typing as t

# ---------------------------------------------------------------------------
# Datasource handles (UIDs match scripts/lgtm/grafana-datasources.yaml).
# ---------------------------------------------------------------------------
PROM: dict[str, str] = {"type": "prometheus", "uid": "prometheus"}
LOKI: dict[str, str] = {"type": "loki", "uid": "loki"}
TEMPO: dict[str, str] = {"type": "tempo", "uid": "tempo"}

# One span per surface invocation — used for "invocations" style panels so
# child spans (search.plan, sqlite.execute, …) don't inflate the count.
ROOT_OPS = (
    "agentgrep.cli.invocation|agentgrep.tui.session|mcp.server.request"
    "|agentgrep.benchmark.run|agentgrep.profile_engine.run"
)

# Common Prometheus label selector fragment driven by template variables.
SCOPE_SELECTOR = 'service_name=~"$service_name", vcs_ref_head_name=~"$branch"'

TAGS = ["agentgrep", "generated"]


def target(
    expr: str,
    legend: str = "",
    *,
    exemplar: bool = False,
    fmt: str = "time_series",
    instant: bool = False,
    ref: str = "A",
    datasource: dict[str, str] | None = None,
) -> dict[str, t.Any]:
    """Build a Prometheus query target.

    Parameters
    ----------
    expr : str
        PromQL expression.
    legend : str
        Legend format string (Grafana ``{{label}}`` templating).
    exemplar : bool
        Request exemplar overlay (metric→trace pivot).
    fmt : str
        ``time_series``, ``heatmap``, or ``table``.
    instant : bool
        Instant (single-point) query instead of a range query.
    ref : str
        Query ref id; unique within a panel.
    datasource : dict or None
        Datasource handle; defaults to Prometheus.

    Returns
    -------
    dict
        A Grafana target object.
    """
    tgt: dict[str, t.Any] = {
        "datasource": datasource or PROM,
        "editorMode": "code",
        "expr": expr,
        "legendFormat": legend or "__auto",
        "range": not instant,
        "instant": instant,
        "refId": ref,
    }
    if exemplar:
        tgt["exemplar"] = True
    if fmt != "time_series":
        tgt["format"] = fmt
    return tgt


class Board:
    """A single dashboard with an auto-flowing 24-column grid layout."""

    def __init__(
        self,
        uid: str,
        title: str,
        *,
        description: str = "",
        refresh: str = "30s",
        time_from: str = "now-6h",
    ) -> None:
        self.uid = uid
        self.title = title
        self.description = description
        self.refresh = refresh
        self.time_from = time_from
        self._panels: list[dict[str, t.Any]] = []
        self._templates: list[dict[str, t.Any]] = []
        self._id = 0
        self._x = 0
        self._y = 0
        self._row_h = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _place(self, w: int, h: int) -> dict[str, int]:
        if self._x + w > 24:
            self._x = 0
            self._y += self._row_h
            self._row_h = 0
        pos = {"x": self._x, "y": self._y, "w": w, "h": h}
        self._x += w
        self._row_h = max(self._row_h, h)
        return pos

    def row(self, title: str) -> None:
        """Start a new full-width collapsible-free row header."""
        if self._x != 0:
            self._x = 0
            self._y += self._row_h
            self._row_h = 0
        self._panels.append(
            {
                "type": "row",
                "title": title,
                "collapsed": False,
                "id": self._next_id(),
                "gridPos": {"x": 0, "y": self._y, "w": 24, "h": 1},
                "panels": [],
            },
        )
        self._y += 1

    def add(self, panel: dict[str, t.Any], *, w: int = 12, h: int = 8) -> None:
        """Place a panel at the current grid cursor."""
        panel["id"] = self._next_id()
        panel["gridPos"] = self._place(w, h)
        self._panels.append(panel)

    # -- template variables -------------------------------------------------
    def var_query(
        self,
        name: str,
        label: str,
        metric: str = "agentgrep_span_count_total",
        *,
        include_all: bool = True,
    ) -> None:
        """Add a Prometheus ``label_values`` template variable."""
        self._templates.append(
            {
                "name": name,
                "label": label,
                "type": "query",
                "datasource": PROM,
                "query": {
                    "qryType": 1,
                    "query": f"label_values({metric}, {name})",
                    "refId": f"var-{name}",
                },
                "refresh": 2,
                "sort": 1,
                "includeAll": include_all,
                "allValue": ".*" if include_all else None,
                "multi": include_all,
                "current": (
                    {"text": "All", "value": "$__all"} if include_all else {"text": "", "value": ""}
                ),
            },
        )

    def to_dict(self) -> dict[str, t.Any]:
        """Render the dashboard envelope."""
        return {
            "uid": self.uid,
            "title": self.title,
            "description": self.description,
            "tags": TAGS,
            "timezone": "browser",
            "schemaVersion": 39,
            "version": 1,
            "editable": True,
            "graphTooltip": 1,
            "refresh": self.refresh,
            "time": {"from": self.time_from, "to": "now"},
            "templating": {"list": self._templates},
            "links": [
                {
                    "title": "agentgrep dashboards",
                    "type": "dashboards",
                    "tags": ["agentgrep"],
                    "asDropdown": True,
                    "includeVars": True,
                    "keepTime": True,
                    "icon": "external link",
                },
            ],
            "annotations": {"list": []},
            "panels": self._panels,
        }


# ---------------------------------------------------------------------------
# Panel builders.
# ---------------------------------------------------------------------------
def _defaults(unit: str, *, color_mode: str = "palette-classic") -> dict[str, t.Any]:
    return {
        "color": {"mode": color_mode},
        "unit": unit,
        "custom": {"fillOpacity": 10, "showPoints": "never", "lineWidth": 2},
    }


def timeseries(
    title: str,
    targets: list[dict[str, t.Any]],
    *,
    unit: str = "short",
    description: str = "",
    stacking: bool = False,
    legend_table: bool = False,
) -> dict[str, t.Any]:
    """Build a timeseries panel."""
    custom = {"fillOpacity": 18 if stacking else 10, "showPoints": "never", "lineWidth": 2}
    if stacking:
        custom["stacking"] = {"mode": "normal", "group": "A"}
    legend = (
        {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull", "max"]}
        if legend_table
        else {"displayMode": "list", "placement": "bottom", "calcs": []}
    )
    return {
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": PROM,
        "targets": targets,
        "fieldConfig": {
            "defaults": {"color": {"mode": "palette-classic"}, "unit": unit, "custom": custom},
            "overrides": [],
        },
        "options": {"legend": legend, "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def stat(
    title: str,
    targets: list[dict[str, t.Any]],
    *,
    unit: str = "short",
    description: str = "",
    thresholds: list[dict[str, t.Any]] | None = None,
    color_mode: str = "value",
    text_mode: str = "auto",
    graph: bool = True,
) -> dict[str, t.Any]:
    """Build a stat panel."""
    field: dict[str, t.Any] = {"unit": unit, "color": {"mode": "thresholds"}}
    if thresholds is not None:
        field["thresholds"] = {"mode": "absolute", "steps": thresholds}
    else:
        field["color"] = {"mode": "palette-classic"}
    return {
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": PROM,
        "targets": targets,
        "fieldConfig": {"defaults": field, "overrides": []},
        "options": {
            "colorMode": color_mode,
            "graphMode": "area" if graph else "none",
            "justifyMode": "auto",
            "textMode": text_mode,
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
    }


def heatmap(
    title: str,
    expr: str,
    *,
    unit: str = "s",
    description: str = "",
) -> dict[str, t.Any]:
    """Build a latency heatmap from cumulative histogram buckets."""
    return {
        "type": "heatmap",
        "title": title,
        "description": description,
        "datasource": PROM,
        "targets": [target(expr, "{{le}}", fmt="heatmap")],
        "options": {
            "calculate": False,
            "cellGap": 1,
            "color": {"scheme": "Spectral", "mode": "scheme", "steps": 64},
            "yAxis": {"unit": unit, "axisPlacement": "left"},
            "tooltip": {"show": True, "yHistogram": True},
            "legend": {"show": True},
        },
        "fieldConfig": {"defaults": {"custom": {"scaleDistribution": {"type": "linear"}}}},
    }


def piechart(
    title: str,
    targets: list[dict[str, t.Any]],
    *,
    unit: str = "short",
    description: str = "",
) -> dict[str, t.Any]:
    """Build a pie chart panel."""
    return {
        "type": "piechart",
        "title": title,
        "description": description,
        "datasource": PROM,
        "targets": targets,
        "fieldConfig": {
            "defaults": {"unit": unit, "color": {"mode": "palette-classic"}},
            "overrides": [],
        },
        "options": {
            "legend": {
                "displayMode": "table",
                "placement": "right",
                "values": ["value", "percent"],
            },
            "pieType": "donut",
            "reduceOptions": {"calcs": ["lastNotNull"], "values": False},
        },
    }


def table_by_operation(
    title: str,
    selector: str,
    *,
    description: str = "",
) -> dict[str, t.Any]:
    """Build an operation-level summary table (count / errors / p95).

    Joins three instant queries on the ``operation`` label and organizes
    them into a compact per-operation breakdown over the dashboard range.
    """
    base = f"agentgrep_span_count_total{{{selector}}}"
    bucket = f"agentgrep_span_duration_seconds_bucket{{{selector}}}"
    targets = [
        target(
            f"sum by (operation) (increase({base}[$__range]))",
            instant=True,
            fmt="table",
            ref="calls",
        ),
        target(
            f'sum by (operation) (increase(agentgrep_span_count_total{{{selector}, outcome="error"}}[$__range]))',
            instant=True,
            fmt="table",
            ref="errors",
        ),
        target(
            f"histogram_quantile(0.95, sum by (operation, le) (rate({bucket}[$__range])))",
            instant=True,
            fmt="table",
            ref="p95",
        ),
    ]
    return {
        "type": "table",
        "title": title,
        "description": description,
        "datasource": PROM,
        "targets": targets,
        "transformations": [
            {"id": "merge", "options": {}},
            {
                "id": "organize",
                "options": {
                    "excludeByName": {"Time": True},
                    "renameByName": {
                        "operation": "Operation",
                        "Value #calls": "Calls",
                        "Value #errors": "Errors",
                        "Value #p95": "p95 (s)",
                    },
                    "indexByName": {
                        "operation": 0,
                        "Value #calls": 1,
                        "Value #errors": 2,
                        "Value #p95": 3,
                    },
                },
            },
        ],
        "fieldConfig": {
            "defaults": {"custom": {"align": "auto", "filterable": True}},
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "p95 (s)"},
                    "properties": [{"id": "unit", "value": "s"}],
                },
                {
                    "matcher": {"id": "byName", "options": "Errors"},
                    "properties": [
                        {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                        {
                            "id": "thresholds",
                            "value": {
                                "mode": "absolute",
                                "steps": [
                                    {"color": "green", "value": None},
                                    {"color": "red", "value": 1},
                                ],
                            },
                        },
                    ],
                },
            ],
        },
        "options": {"showHeader": True, "sortBy": [{"displayName": "Calls", "desc": True}]},
    }


# Threshold palettes.
ERR_THRESHOLDS = [
    {"color": "green", "value": None},
    {"color": "yellow", "value": 0.01},
    {"color": "red", "value": 0.05},
]
LAT_THRESHOLDS = [
    {"color": "green", "value": None},
    {"color": "yellow", "value": 0.5},
    {"color": "red", "value": 2},
]


# ---------------------------------------------------------------------------
# Dashboards.
# ---------------------------------------------------------------------------
def build_overview() -> Board:
    """RED overview across every surface."""
    b = Board(
        "agentgrep-overview",
        "agentgrep · Overview (RED)",
        description="Rate, errors, and duration across every agentgrep surface, "
        "driven by the generic span instruments.",
    )
    b.var_query("service_name", "Service")
    b.var_query("branch", "Branch", metric="agentgrep_span_count_total")
    b.var_query("operation", "Operation")

    sel = SCOPE_SELECTOR
    op_sel = f'{sel}, operation=~"$operation"'

    b.row("At a glance")
    b.add(
        stat(
            "Invocations (range)",
            [
                target(
                    f'sum(increase(agentgrep_span_count_total{{{sel}, operation=~"{ROOT_OPS}"}}[$__range]))'
                )
            ],
            unit="short",
            description="Surface entry-point spans over the selected range.",
            color_mode="value",
        ),
        w=5,
        h=5,
    )
    b.add(
        stat(
            "Error rate",
            [
                target(
                    f'sum(rate(agentgrep_span_count_total{{{op_sel}, outcome="error"}}[$__rate_interval]))'
                    f" / clamp_min(sum(rate(agentgrep_span_count_total{{{op_sel}}}[$__rate_interval])), 0.0001)",
                )
            ],
            unit="percentunit",
            thresholds=ERR_THRESHOLDS,
            description="Fraction of spans ending in error.",
        ),
        w=5,
        h=5,
    )
    b.add(
        stat(
            "p95 latency",
            [
                target(
                    f"histogram_quantile(0.95, sum by (le) "
                    f"(rate(agentgrep_span_duration_seconds_bucket{{{op_sel}}}[$__rate_interval])))"
                )
            ],
            unit="s",
            thresholds=LAT_THRESHOLDS,
            description="95th percentile span duration.",
        ),
        w=5,
        h=5,
    )
    b.add(
        stat(
            "Build",
            [
                target(
                    f"count by (service_version, vcs_ref_head_name) "
                    f"(agentgrep_span_count_total{{{sel}}})",
                    "{{vcs_ref_head_name}} @ {{service_version}}",
                    instant=True,
                )
            ],
            unit="short",
            text_mode="name",
            graph=False,
            description="Active service version and VCS branch reporting data.",
        ),
        w=9,
        h=5,
    )

    b.row("Throughput & errors")
    b.add(
        timeseries(
            "Span rate by operation",
            [
                target(
                    f"sum by (operation) (rate(agentgrep_span_count_total{{{op_sel}}}[$__rate_interval]))",
                    "{{operation}}",
                )
            ],
            unit="reqps",
            legend_table=True,
            description="Completed spans per second, faceted by operation.",
        ),
        w=12,
        h=9,
    )
    b.add(
        timeseries(
            "Error rate by operation",
            [
                target(
                    f'sum by (operation) (rate(agentgrep_span_count_total{{{op_sel}, outcome="error"}}[$__rate_interval]))',
                    "{{operation}}",
                )
            ],
            unit="reqps",
            legend_table=True,
            description="Errored spans per second, faceted by operation.",
        ),
        w=12,
        h=9,
    )

    b.row("Latency (exemplars → traces)")
    b.add(
        timeseries(
            "Span latency p50 / p90 / p99",
            [
                target(
                    f"histogram_quantile(0.50, sum by (le) (rate(agentgrep_span_duration_seconds_bucket{{{op_sel}}}[$__rate_interval])))",
                    "p50",
                    exemplar=True,
                    ref="A",
                ),
                target(
                    f"histogram_quantile(0.90, sum by (le) (rate(agentgrep_span_duration_seconds_bucket{{{op_sel}}}[$__rate_interval])))",
                    "p90",
                    ref="B",
                ),
                target(
                    f"histogram_quantile(0.99, sum by (le) (rate(agentgrep_span_duration_seconds_bucket{{{op_sel}}}[$__rate_interval])))",
                    "p99",
                    ref="C",
                ),
            ],
            unit="s",
            description="Click an exemplar diamond to jump to the originating trace.",
        ),
        w=12,
        h=9,
    )
    b.add(
        heatmap(
            "Span latency distribution",
            f"sum by (le) (rate(agentgrep_span_duration_seconds_bucket{{{op_sel}}}[$__rate_interval]))",
            unit="s",
        ),
        w=12,
        h=9,
    )

    b.row("Operation breakdown")
    b.add(table_by_operation("Per-operation summary (range)", sel), w=24, h=9)
    return b


def build_search() -> Board:
    """Search engine deep dive."""
    b = Board(
        "agentgrep-search",
        "agentgrep · Search Engine",
        description="Search latency, work volume (sources scanned, results "
        "returned), and the SQLite cost inside the search path.",
    )
    b.var_query("service_name", "Service")
    b.var_query("branch", "Branch")
    b.var_query("scope", "Scope", metric="agentgrep_search_sources_count")

    sel = SCOPE_SELECTOR
    eng = f'{sel}, agentgrep_scope=~"$scope"'

    b.row("Search rate & latency")
    b.add(
        stat(
            "Searches (range)",
            [target(f"sum(increase(agentgrep_search_sources_count{{{eng}}}[$__range]))")],
            description="Search executions over the range.",
            color_mode="value",
        ),
        w=4,
        h=5,
    )
    b.add(
        stat(
            "p95 search.run",
            [
                target(
                    f'histogram_quantile(0.95, sum by (le) (rate(agentgrep_span_duration_seconds_bucket{{{sel}, operation="agentgrep.search.run"}}[$__rate_interval])))'
                )
            ],
            unit="s",
            thresholds=LAT_THRESHOLDS,
        ),
        w=4,
        h=5,
    )
    b.add(
        stat(
            "p95 sources scanned",
            [
                target(
                    f"histogram_quantile(0.95, sum by (le) (rate(agentgrep_search_sources_bucket{{{eng}}}[$__rate_interval])))"
                )
            ],
            unit="short",
            color_mode="value",
        ),
        w=4,
        h=5,
    )
    b.add(
        timeseries(
            "Search phase latency p95",
            [
                target(
                    f'histogram_quantile(0.95, sum by (operation, le) (rate(agentgrep_span_duration_seconds_bucket{{{sel}, operation=~"search\\\\..*|agentgrep.search.run"}}[$__rate_interval])))',
                    "{{operation}}",
                    exemplar=True,
                )
            ],
            unit="s",
            legend_table=True,
            description="discover / plan / collect phases plus the run root.",
        ),
        w=12,
        h=5,
    )

    b.row("Work volume")
    b.add(
        timeseries(
            "Sources scanned (p50/p95) ",
            [
                target(
                    f"histogram_quantile(0.50, sum by (le) (rate(agentgrep_search_sources_bucket{{{eng}}}[$__rate_interval])))",
                    "p50",
                    ref="A",
                ),
                target(
                    f"histogram_quantile(0.95, sum by (le) (rate(agentgrep_search_sources_bucket{{{eng}}}[$__rate_interval])))",
                    "p95",
                    ref="B",
                ),
            ],
            unit="short",
            description="How many sources each search touched.",
        ),
        w=8,
        h=8,
    )
    b.add(
        timeseries(
            "Results returned (p50/p95)",
            [
                target(
                    f"histogram_quantile(0.50, sum by (le) (rate(agentgrep_search_results_bucket{{{eng}}}[$__rate_interval])))",
                    "p50",
                    ref="A",
                ),
                target(
                    f"histogram_quantile(0.95, sum by (le) (rate(agentgrep_search_results_bucket{{{eng}}}[$__rate_interval])))",
                    "p95",
                    ref="B",
                ),
            ],
            unit="short",
        ),
        w=8,
        h=8,
    )
    b.add(
        piechart(
            "Searches by scope",
            [
                target(
                    f"sum by (agentgrep_scope) (increase(agentgrep_search_results_count{{{eng}}}[$__range]))",
                    "{{agentgrep_scope}}",
                    instant=True,
                )
            ],
            description="prompts vs conversations split.",
        ),
        w=8,
        h=8,
    )

    b.row("SQLite cost (Cursor backend)")
    b.add(
        timeseries(
            "SQLite calls by method",
            [
                target(
                    f"sum by (agentgrep_sql_method) (rate(agentgrep_otel_sqlite_total{{{sel}}}[$__rate_interval]))",
                    "{{agentgrep_sql_method}}",
                )
            ],
            unit="reqps",
            stacking=True,
        ),
        w=12,
        h=8,
    )
    b.add(
        timeseries(
            "sqlite.execute latency p95",
            [
                target(
                    f'histogram_quantile(0.95, sum by (le) (rate(agentgrep_span_duration_seconds_bucket{{{sel}, operation="agentgrep.sqlite.execute"}}[$__rate_interval])))',
                    "p95",
                    exemplar=True,
                )
            ],
            unit="s",
        ),
        w=12,
        h=8,
    )
    return b


def build_find_grep() -> Board:
    """Find enumeration and grep selectivity."""
    b = Board(
        "agentgrep-find-grep",
        "agentgrep · Find & Grep",
        description="Source enumeration (find) and grep-shaped scan volume, "
        "latency, and match selectivity.",
    )
    b.var_query("service_name", "Service")
    b.var_query("branch", "Branch")
    sel = SCOPE_SELECTOR

    b.row("Find")
    b.add(
        timeseries(
            "Find latency p95",
            [
                target(
                    f'histogram_quantile(0.95, sum by (operation, le) (rate(agentgrep_span_duration_seconds_bucket{{{sel}, operation=~"find\\\\..*"}}[$__rate_interval])))',
                    "{{operation}}",
                    exemplar=True,
                )
            ],
            unit="s",
            legend_table=True,
        ),
        w=12,
        h=8,
    )
    b.add(
        timeseries(
            "Find sources & results (p95)",
            [
                target(
                    f"histogram_quantile(0.95, sum by (le) (rate(agentgrep_find_sources_bucket{{{sel}}}[$__rate_interval])))",
                    "sources p95",
                    ref="A",
                ),
                target(
                    f"histogram_quantile(0.95, sum by (le) (rate(agentgrep_find_results_bucket{{{sel}}}[$__rate_interval])))",
                    "results p95",
                    ref="B",
                ),
            ],
            unit="short",
        ),
        w=12,
        h=8,
    )

    b.row("Grep")
    b.add(
        stat(
            "p95 grep duration",
            [
                target(
                    f"histogram_quantile(0.95, sum by (le) (rate(agentgrep_grep_duration_bucket{{{sel}}}[$__rate_interval])))"
                )
            ],
            unit="ms",
            thresholds=[
                {"color": "green", "value": None},
                {"color": "yellow", "value": 500},
                {"color": "red", "value": 2000},
            ],
            description="grep-shaped scan wall time.",
        ),
        w=6,
        h=7,
    )
    b.add(
        stat(
            "Match selectivity",
            [
                target(
                    f"sum(rate(agentgrep_grep_emitted_count_total{{{sel}}}[$__rate_interval])) "
                    f"/ clamp_min(sum(rate(agentgrep_grep_candidate_count_total{{{sel}}}[$__rate_interval])), 0.0001)"
                )
            ],
            unit="percentunit",
            color_mode="value",
            description="Emitted matches ÷ candidates scanned.",
        ),
        w=6,
        h=7,
    )
    b.add(
        timeseries(
            "Grep candidates vs emitted",
            [
                target(
                    f"sum(rate(agentgrep_grep_candidate_count_total{{{sel}}}[$__rate_interval]))",
                    "candidates",
                    ref="A",
                ),
                target(
                    f"sum(rate(agentgrep_grep_emitted_count_total{{{sel}}}[$__rate_interval]))",
                    "emitted",
                    ref="B",
                ),
            ],
            unit="reqps",
            description="Scan funnel: candidates considered vs matches emitted.",
        ),
        w=12,
        h=7,
    )
    return b


def build_mcp() -> Board:
    """MCP server surface (tools, requests, flush)."""
    b = Board(
        "agentgrep-mcp",
        "agentgrep · MCP Server",
        description="MCP request and tool-call rate, latency, and errors. "
        "Populates once the agentgrep-mcp service reports data.",
    )
    b.var_query("service_name", "Service")
    b.var_query("branch", "Branch")
    sel = SCOPE_SELECTOR
    mcp = f'{sel}, operation=~"mcp\\\\..*|agentgrep.mcp..*"'

    b.row("Requests & tools")
    b.add(
        timeseries(
            "MCP span rate by operation",
            [
                target(
                    f"sum by (operation) (rate(agentgrep_span_count_total{{{mcp}}}[$__rate_interval]))",
                    "{{operation}}",
                )
            ],
            unit="reqps",
            legend_table=True,
        ),
        w=12,
        h=8,
    )
    b.add(
        timeseries(
            "MCP latency p95 by operation",
            [
                target(
                    f"histogram_quantile(0.95, sum by (operation, le) (rate(agentgrep_span_duration_seconds_bucket{{{mcp}}}[$__rate_interval])))",
                    "{{operation}}",
                    exemplar=True,
                )
            ],
            unit="s",
            legend_table=True,
        ),
        w=12,
        h=8,
    )

    b.row("Tool detail & flush")
    b.add(
        timeseries(
            "Tool call rate by tool",
            [
                target(
                    f'sum by (agentgrep_tool) (rate(agentgrep_span_count_total{{{sel}, agentgrep_tool!=""}}[$__rate_interval]))',
                    "{{agentgrep_tool}}",
                )
            ],
            unit="reqps",
            stacking=True,
            description="Per-MCP-tool call rate (agentgrep_tool label).",
        ),
        w=12,
        h=8,
    )
    b.add(
        timeseries(
            "MCP flush duration p95",
            [
                target(
                    f"histogram_quantile(0.95, sum by (le) (rate(agentgrep_mcp_flush_duration_bucket{{{sel}}}[$__rate_interval])))",
                    "p95",
                )
            ],
            unit="ms",
            description="Telemetry flush latency on MCP shutdown.",
        ),
        w=12,
        h=8,
    )
    return b


def build_surfaces() -> Board:
    """Cross-surface and version comparison."""
    b = Board(
        "agentgrep-surfaces",
        "agentgrep · Surfaces & Versions",
        description="Compare CLI, TUI, MCP, engine, and benchmark surfaces, "
        "and watch behavior shift across branches and versions.",
    )
    b.var_query("branch", "Branch")
    sel = 'vcs_ref_head_name=~"$branch"'

    b.row("By surface")
    b.add(
        timeseries(
            "Span rate by surface",
            [
                target(
                    f"sum by (agentgrep_surface) (rate(agentgrep_span_count_total{{{sel}}}[$__rate_interval]))",
                    "{{agentgrep_surface}}",
                )
            ],
            unit="reqps",
            stacking=True,
        ),
        w=12,
        h=8,
    )
    b.add(
        timeseries(
            "p95 latency by surface",
            [
                target(
                    f"histogram_quantile(0.95, sum by (agentgrep_surface, le) (rate(agentgrep_span_duration_seconds_bucket{{{sel}}}[$__rate_interval])))",
                    "{{agentgrep_surface}}",
                )
            ],
            unit="s",
            legend_table=True,
        ),
        w=12,
        h=8,
    )

    b.row("By service & version")
    b.add(
        piechart(
            "Spans by service",
            [
                target(
                    f"sum by (service_name) (increase(agentgrep_span_count_total{{{sel}}}[$__range]))",
                    "{{service_name}}",
                    instant=True,
                )
            ],
        ),
        w=8,
        h=8,
    )
    b.add(
        timeseries(
            "TUI results returned (p95)",
            [
                target(
                    f"histogram_quantile(0.95, sum by (le) (rate(agentgrep_tui_search_results_bucket{{{sel}}}[$__rate_interval])))",
                    "p95",
                )
            ],
            unit="short",
        ),
        w=8,
        h=8,
    )
    b.add(
        timeseries(
            "Error rate by service",
            [
                target(
                    f'sum by (service_name) (rate(agentgrep_span_count_total{{{sel}, outcome="error"}}[$__rate_interval]))',
                    "{{service_name}}",
                )
            ],
            unit="reqps",
            legend_table=True,
        ),
        w=8,
        h=8,
    )
    return b


def build_agentic() -> Board:
    """Agentic debug-session correlation board.

    Centerpiece for AI agent debugging loops: filter to a single run by
    its debug session id (set via ``AGENTGREP_DEBUG_SESSION_ID``) and see
    that run's operations, errors, and trace-linked logs together.
    """
    b = Board(
        "agentgrep-agentic",
        "agentgrep · Agentic Debug Session",
        description="Pivot one agent run end to end: its operations, "
        "outcomes, latency, and trace-linked logs. Filter by debug session, "
        "attempt, or pytest run id.",
        refresh="10s",
        time_from="now-1h",
    )
    b.var_query("session", "Debug session", metric="agentgrep_span_count_total", include_all=True)
    b.var_query("service_name", "Service")

    # When no debug session is active the label is absent; match-any keeps
    # the board populated so it's useful even outside an instrumented loop.
    sel = 'service_name=~"$service_name"'

    b.row("This run")
    b.add(
        stat(
            "Spans (range)",
            [target(f"sum(increase(agentgrep_span_count_total{{{sel}}}[$__range]))")],
            color_mode="value",
        ),
        w=6,
        h=5,
    )
    b.add(
        stat(
            "Errors (range)",
            [
                target(
                    f'sum(increase(agentgrep_span_count_total{{{sel}, outcome="error"}}[$__range]))'
                )
            ],
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
        ),
        w=6,
        h=5,
    )
    b.add(
        stat(
            "p95 latency",
            [
                target(
                    f"histogram_quantile(0.95, sum by (le) (rate(agentgrep_span_duration_seconds_bucket{{{sel}}}[$__rate_interval])))"
                )
            ],
            unit="s",
            thresholds=LAT_THRESHOLDS,
        ),
        w=6,
        h=5,
    )
    b.add(
        stat(
            "Active services",
            [
                target(
                    f"count(count by (service_name) (agentgrep_span_count_total{{{sel}}}))",
                    instant=True,
                )
            ],
            color_mode="value",
            graph=False,
        ),
        w=6,
        h=5,
    )

    b.row("Operations in this run")
    b.add(table_by_operation("Operation timeline (range)", sel), w=24, h=9)

    b.row("Trace-linked logs (Loki)")
    b.add(
        {
            "type": "logs",
            "title": "Telemetry logs (click trace_id → Tempo)",
            "description": "Structured app logs exported through OTel, linked "
            "to traces by the trace_id derived field.",
            "datasource": LOKI,
            "targets": [
                target(
                    '{service_name=~"$service_name"} | json',
                    fmt="logs",
                    datasource=LOKI,
                )
            ],
            "options": {
                "showTime": True,
                "wrapLogMessage": True,
                "enableLogDetails": True,
                "dedupStrategy": "none",
                "sortOrder": "Descending",
            },
        },
        w=24,
        h=10,
    )
    return b


DASHBOARDS: tuple[t.Callable[[], Board], ...] = (
    build_overview,
    build_search,
    build_find_grep,
    build_mcp,
    build_surfaces,
    build_agentic,
)


def write_dashboards(out_dir: pathlib.Path) -> list[pathlib.Path]:
    """Render and write every dashboard JSON into ``out_dir``.

    Parameters
    ----------
    out_dir : pathlib.Path
        Destination directory; created if missing.

    Returns
    -------
    list of pathlib.Path
        The written file paths, one per dashboard.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[pathlib.Path] = []
    for factory in DASHBOARDS:
        board = factory()
        path = out_dir / f"{board.uid}.json"
        path.write_text(json.dumps(board.to_dict(), indent=2, sort_keys=True) + "\n")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    default_out = pathlib.Path(__file__).resolve().parent / "dashboards"
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=default_out,
        help="Directory to write dashboard JSON into (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    paths = write_dashboards(args.output)
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
