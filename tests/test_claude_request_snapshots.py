"""Claude Code request snapshots must not surface as conversation records.

From Claude Code 2.1.310, a transcript also holds ``api-request``,
``api-request-shape``, and ``api-request-blob`` records. A blob carries a
``message`` copy of what the request sent, including the system prompt and
injected context, so reading it as a turn returns each message twice and
returns text the user never typed.
"""

from __future__ import annotations

import pathlib

from agentgrep.adapters.claude import parse_claude_project_file
from agentgrep.records import SourceHandle

from .conftest import fixture_path


def test_claude_request_blobs_are_not_records(tmp_path: pathlib.Path) -> None:
    """Only the user's own turn is returned from a transcript with blobs."""
    transcript = (
        tmp_path / "projects" / "-work-example" / "00000000-0000-0000-0000-000000000000.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        fixture_path("claude.projects.session", "request-snapshot.jsonl").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    source = SourceHandle(
        agent="claude",
        store="claude.projects",
        adapter_id="claude.projects_jsonl.v1",
        path=transcript,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=0,
    )

    records = list(parse_claude_project_file(source))

    assert [(record.role, record.text) for record in records] == [
        ("user", "<redacted user prompt>"),
    ]
