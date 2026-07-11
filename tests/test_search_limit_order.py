"""Regression pin for ``limit`` versus result ordering.

Search results are ranked newest-first, and the unlimited path honours that:
a record ranks by its own timestamp no matter which file it came from. The
limited path does not. ``limit`` is a count cutoff the execution driver applies
while it walks sources in source-mtime order, and the newest-first sort is a
post-hoc pass over whatever survived that cutoff — so a record living in an
older-mtime source is discarded before the ranker ever compares it.

The fixture makes the two orders disagree on purpose. One source has the newer
file mtime but the older prompt; the other has the older file mtime but the
newest prompt. Scan order and result order therefore nominate different
winners, and ``limit=1`` returns the scan-order winner instead of the ranked
one. That case is a strict xfail: the fix is deferred to #113, and
``xfail_strict`` means landing the fix makes this test fail until the marker
goes with it.
"""

from __future__ import annotations

import json
import os
import pathlib
import typing as t

import pytest

import agentgrep

OLDER_PROMPT = "limit-order older prompt"
NEWEST_PROMPT = "limit-order newest prompt"

# Only the relative order of these matters: the source holding the OLDER prompt
# is stamped newer on disk, so source-mtime scan order reaches it first.
NEWER_SOURCE_MTIME_NS = 1_800_000_000_000_000_000
OLDER_SOURCE_MTIME_NS = 1_700_000_000_000_000_000

LIMIT_GAP = (
    "limit is a count cutoff the driver applies while walking sources in "
    "source-mtime order, and newest-first ordering is a post-hoc sort over "
    "whatever survived the cutoff. The newest record lives in the older-mtime "
    "source, so the driver stops before it is read and the ranker never "
    "compares it -- while the CLI --limit help promises a cut applied 'after "
    "ranking'. Fixing this means ranking before the cutoff (or a bounded "
    "newest-first merge across sources); deliberately deferred to #113."
)


class CodexSession(t.NamedTuple):
    """One synthetic Codex session file: its prompt and its on-disk mtime."""

    name: str
    timestamp: str
    text: str
    mtime_ns: int


NEWER_MTIME_OLDER_PROMPT = CodexSession(
    name="newer-mtime-older-prompt.jsonl",
    timestamp="2026-01-01T00:00:00Z",
    text=OLDER_PROMPT,
    mtime_ns=NEWER_SOURCE_MTIME_NS,
)
OLDER_MTIME_NEWEST_PROMPT = CodexSession(
    name="older-mtime-newest-prompt.jsonl",
    timestamp="2026-06-01T00:00:00Z",
    text=NEWEST_PROMPT,
    mtime_ns=OLDER_SOURCE_MTIME_NS,
)


class LimitOrderCase(t.NamedTuple):
    """One ``limit`` value and the newest-first records it must return."""

    test_id: str
    limit: int | None
    expected_texts: tuple[str, ...]
    gap: str | None


LIMIT_ORDER_CASES = (
    LimitOrderCase(
        test_id="unlimited-ranks-newest-first",
        limit=None,
        expected_texts=(NEWEST_PROMPT, OLDER_PROMPT),
        gap=None,
    ),
    LimitOrderCase(
        test_id="limit-one-keeps-the-newest",
        limit=1,
        expected_texts=(NEWEST_PROMPT,),
        gap=LIMIT_GAP,
    ),
)


def _write_codex_session(home: pathlib.Path, session: CodexSession) -> pathlib.Path:
    """Write one Codex rollout session file and stamp its mtime.

    The rollout JSONL shape is a ``session_meta`` header followed by
    ``response_item`` lines; the record timestamp is read from the line's
    top-level ``timestamp`` key, independently of the file's mtime.

    Parameters
    ----------
    home : pathlib.Path
        Home directory the engine discovers ``.codex/sessions`` under.
    session : CodexSession
        Prompt text, record timestamp, and mtime to stamp on the file.

    Returns
    -------
    pathlib.Path
        The session file that was written.
    """
    path = home / ".codex" / "sessions" / "2026" / "01" / session.name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as out:
        header = {"type": "session_meta", "payload": {"id": session.name}}
        prompt = {
            "type": "response_item",
            "timestamp": session.timestamp,
            "payload": {"role": "user", "content": session.text},
        }
        out.write(f"{json.dumps(header)}\n{json.dumps(prompt)}\n")
    os.utime(path, ns=(session.mtime_ns, session.mtime_ns))
    return path


def _make_query(*, limit: int | None) -> agentgrep.SearchQuery:
    """Build the shared prompt-scope query, varying only ``limit``.

    Parameters
    ----------
    limit : int or None
        Result cap to apply. ``None`` searches without a cap.

    Returns
    -------
    agentgrep.SearchQuery
        A query whose bare term matches the prompt in both sources.
    """
    return agentgrep.SearchQuery(
        terms=("limit-order",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=limit,
        dedupe=True,
    )


@pytest.fixture(name="disagreeing_home")
def fixture_disagreeing_home(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> pathlib.Path:
    """Home whose source-mtime scan order and ranked result order disagree."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    for session in (NEWER_MTIME_OLDER_PROMPT, OLDER_MTIME_NEWEST_PROMPT):
        _ = _write_codex_session(tmp_path, session)
    return tmp_path


@pytest.mark.parametrize(
    LimitOrderCase._fields,
    [
        pytest.param(
            *case,
            marks=(
                ()
                if case.gap is None
                else pytest.mark.xfail(strict=True, reason=case.gap, raises=AssertionError)
            ),
        )
        for case in LIMIT_ORDER_CASES
    ],
    ids=[case.test_id for case in LIMIT_ORDER_CASES],
)
def test_search_limit_keeps_the_newest_records(
    test_id: str,
    limit: int | None,
    expected_texts: tuple[str, ...],
    gap: str | None,
    disagreeing_home: pathlib.Path,
) -> None:
    """``limit`` must cut the ranked result list, not the scan.

    Both sources match the query, so ranking alone decides the winner: the
    newest prompt comes first whether or not a cap is set, and ``limit=1``
    should keep exactly that record. The unlimited case proves the fixture and
    the ranker agree on which record is newest, which is what makes the limited
    case a real defect rather than a broken fixture.
    """
    records = agentgrep.run_search_query(disagreeing_home, _make_query(limit=limit))
    assert [record.text for record in records] == list(expected_texts)
