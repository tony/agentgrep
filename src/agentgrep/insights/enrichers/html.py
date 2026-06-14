"""Level 1 enricher: render the report payload to standalone HTML (jinja2)."""

from __future__ import annotations

import typing as t

from agentgrep.insights.model import InsightsEnrichment

if t.TYPE_CHECKING:
    from agentgrep.insights.enrichers import EnricherContext

_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>agentgrep insights — {{ report.scope }}</title>
<style>
  body { font: 15px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 52rem; }
  h1 { font-size: 1.5rem; } h2 { margin-top: 1.6rem; font-size: 1.15rem; }
  .glance { color: #444; } code { background: #f3f3f3; padding: 0 .25rem; }
  table { border-collapse: collapse; } td, th { padding: .15rem .6rem; text-align: left; }
  .term { display: inline-block; background: #eef; margin: .1rem;
          padding: 0 .4rem; border-radius: .3rem; }
</style>
</head>
<body>
<h1>agentgrep insights</h1>
<p class="glance">{{ report.activity.summary }}
  (level <code>{{ report.level }}</code>, status <code>{{ report.status }}</code>)</p>

<h2>Top terms</h2>
<p>{% for term in report.top_terms %}<span class="term">{{ term.term }} ·
  {{ term.count }}</span>{% endfor %}</p>

<h2>Work areas</h2>
<table>
<tr><th>Area</th><th>Records</th><th>Top terms</th></tr>
{% for area in report.activity.work_areas %}
<tr><td>{{ area.label }}</td><td>{{ area.record_count }}</td>
<td>
{%- for term in area.top_terms -%}
{{ term.term }}{% if not loop.last %}, {% endif %}
{%- endfor -%}
</td></tr>
{% endfor %}
</table>

<h2>Timeline</h2>
<table>
<tr><th>Date</th><th>Records</th></tr>
{% for bucket in report.activity.timeline %}
<tr><td>{{ bucket.date }}</td><td>{{ bucket.record_count }}</td></tr>
{% endfor %}
</table>

{% if report.activity.open_threads %}
<h2>Open threads</h2>
<ul>
{% for thread in report.activity.open_threads %}
<li>{{ thread.title }} <em>({{ thread.agent }})</em></li>
{% endfor %}
</ul>
{% endif %}
</body>
</html>
"""


def build_html(ctx: EnricherContext) -> InsightsEnrichment:
    """Render the report to an HTML document using jinja2."""
    if ctx.progress is not None:
        ctx.progress.phase("render", detail="html")
    jinja2 = ctx.modules["jinja2"]
    template = jinja2.Template(_TEMPLATE, autoescape=True)
    html = template.render(report=ctx.report.to_payload())
    return InsightsEnrichment(
        level="html",
        backend=ctx.backend,
        status="ok",
        message=f"rendered {len(html)} bytes of HTML",
        data={"html": html, "bytes": len(html)},
    )
