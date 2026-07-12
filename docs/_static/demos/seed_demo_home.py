#!/usr/bin/env python3
"""Seed a throwaway ``$HOME`` with synthetic agent history for the demos.

The recordings in ``docs/_static/demos/`` must never show a real prompt.
agentgrep reads whatever agent stores exist under ``$HOME``, so the demos run
against a sandbox home built entirely from the fabricated corpus below: three
invented projects (``webshop``, ``telemetry``, ``parser-lab``) and prompts
about generic software topics.

Store layouts mirror ``tests/samples/``. Run via
``docs/_static/demos/demo-env.sh``, which also neutralizes the non-``$HOME``
escape hatches (notably the WSL ``/mnt/c/Users`` probe).

Usage
-----
``python docs/_static/demos/seed_demo_home.py /tmp/agentgrep-demo``
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import sys
import time
import uuid

# Wall-clock anchor for every synthetic timestamp. Anchored to "two hours ago"
# so the relative times agentgrep renders ("3h ago", "2d ago") stay fresh
# whenever the demos are re-recorded. Set AGENTGREP_DEMO_EPOCH to pin it.
BASE_EPOCH = int(os.environ.get("AGENTGREP_DEMO_EPOCH") or time.time() - 7_200)
DAY = 86_400

PROJECTS = {
    "webshop": "main",
    "telemetry": "feat/otel-spans",
    "parser-lab": "main",
}

DEMO_HOME = pathlib.Path("/tmp/agentgrep-demo")


def _uid(seed: str) -> str:
    """Derive a stable UUID from ``seed`` so re-seeding is reproducible.

    Parameters
    ----------
    seed : str
        Text used to derive the UUID.

    Returns
    -------
    str
        Stable UUID string for the supplied seed.
    """
    return str(uuid.UUID(hashlib.sha256(seed.encode()).hexdigest()[:32]))


def _iso(offset: int) -> str:
    """Render ``BASE_EPOCH + offset`` as a UTC ISO-8601 timestamp.

    Parameters
    ----------
    offset : int
        Number of seconds to add to the base epoch.

    Returns
    -------
    str
        UTC timestamp with millisecond precision.
    """
    import datetime

    stamp = datetime.datetime.fromtimestamp(BASE_EPOCH + offset, tz=datetime.UTC)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _write_jsonl(path: pathlib.Path, rows: list[dict[str, object]]) -> None:
    """Write mappings as newline-delimited JSON.

    Parameters
    ----------
    path : pathlib.Path
        Destination file path.
    rows : list[dict[str, object]]
        Mappings to serialize, one per line.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _write_json(path: pathlib.Path, payload: object) -> None:
    """Write a payload as indented JSON.

    Parameters
    ----------
    path : pathlib.Path
        Destination file path.
    payload : object
        JSON-serializable value to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# --- Codex ------------------------------------------------------------------

CODEX_PROMPTS = [
    "add retry with exponential backoff to the http client",
    "why does the deploy step fail on the staging environment",
    "write a benchmark for the tokenizer hot path",
    "cache the parsed config so startup stops re-reading it",
    "raise the request timeout for the slow report endpoint",
]

CODEX_SESSIONS = [
    {
        "project": "telemetry",
        "model": "gpt-5-codex",
        "user": "instrument the request handler with otel spans",
        "reply": (
            "Wrapped the handler in a tracer span and tagged it with the route. "
            "TODO: the sample rate is still hardcoded to 1.0."
        ),
    },
    {
        "project": "webshop",
        "model": "o4-mini",
        "user": "the checkout deploy keeps hitting the 30s timeout",
        "reply": (
            "The deploy timeout lives in the release config, not the client. "
            "TODO: make it configurable per environment."
        ),
    },
]


def seed_codex(home: pathlib.Path) -> None:
    """Write ``~/.codex/history.jsonl`` and dated session rollouts.

    Parameters
    ----------
    home : pathlib.Path
        Root of the synthetic demo home.
    """
    root = home / ".codex"
    _write_jsonl(
        root / "history.jsonl",
        [
            {
                "session_id": _uid(f"codex-hist-{index}"),
                "ts": BASE_EPOCH - (len(CODEX_PROMPTS) - index) * 3_600,
                "text": text,
            }
            for index, text in enumerate(CODEX_PROMPTS)
        ],
    )

    for index, session in enumerate(CODEX_SESSIONS):
        offset = -(index + 1) * DAY
        session_id = _uid(f"codex-sess-{index}")
        cwd = str(home / "code" / session["project"])
        stamp = _iso(offset)
        day = stamp[:10].replace("-", "/")
        rollout = (
            root / "sessions" / day / f"rollout-{stamp[:19].replace(':', '-')}-{session_id}.jsonl"
        )
        _write_jsonl(
            rollout,
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": session_id,
                        "timestamp": stamp,
                        "cwd": cwd,
                        "originator": "codex_cli_rs",
                        "cli_version": "0.55.0",
                        "source": "cli",
                        "git": {
                            "branch": PROJECTS[session["project"]],
                            "commit": hashlib.sha1(session_id.encode()).hexdigest(),
                        },
                    },
                },
                {"type": "turn_context", "payload": {"model": session["model"]}},
                {
                    "type": "response_item",
                    "payload": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": session["user"]}],
                        "timestamp": _iso(offset + 1),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": session["reply"]}],
                        "timestamp": _iso(offset + 4),
                    },
                },
            ],
        )


# --- Claude Code ------------------------------------------------------------

# (prompt, project) -- spread across projects so `project:` and `--cwd`
# filters visibly narrow the result set instead of matching everything.
CLAUDE_PROMPTS = [
    ("refactor the parser to stream tokens instead of buffering", "parser-lab"),
    ("add a retry budget so we stop hammering the api", "telemetry"),
    ("document the caching layer in the readme", "webshop"),
    ("roll back the deploy when the health check fails", "webshop"),
    ("set up a github actions matrix for python 3.13 and 3.14", "parser-lab"),
]

CLAUDE_SESSIONS = [
    {
        "project": "parser-lab",
        "model": "claude-opus-4-8",
        "user": "the parser drops trailing whitespace, add a failing test first",
        "reply": (
            "Added a failing test for trailing whitespace, then fixed the lexer "
            "to emit it. TODO: cover CRLF line endings too."
        ),
    },
    {
        "project": "webshop",
        "model": "claude-sonnet-5",
        "user": "add a cache layer in front of the product catalog query",
        "reply": (
            "Added a read-through cache keyed by catalog id, with a 60s TTL "
            "and a retry on a cold miss."
        ),
    },
]


def seed_claude(home: pathlib.Path) -> None:
    """Write ``~/.claude/history.jsonl`` and per-project session transcripts.

    Parameters
    ----------
    home : pathlib.Path
        Root of the synthetic demo home.
    """
    root = home / ".claude"
    _write_jsonl(
        root / "history.jsonl",
        [
            {
                "display": text,
                "pastedContents": {},
                "timestamp": (BASE_EPOCH - (len(CLAUDE_PROMPTS) - i) * 5_400) * 1000,
                "project": str(home / "code" / project),
                "sessionId": _uid(f"claude-hist-{i}"),
            }
            for i, (text, project) in enumerate(CLAUDE_PROMPTS)
        ],
    )

    for index, session in enumerate(CLAUDE_SESSIONS):
        offset = -(index + 1) * DAY - 3_600
        session_id = _uid(f"claude-sess-{index}")
        cwd = home / "code" / session["project"]
        encoded = str(cwd).replace("/", "-")
        user_uuid = _uid(f"claude-user-{index}")
        _write_jsonl(
            root / "projects" / encoded / f"{session_id}.jsonl",
            [
                {
                    "parentUuid": None,
                    "isSidechain": False,
                    "type": "user",
                    "uuid": user_uuid,
                    "timestamp": _iso(offset),
                    "userType": "external",
                    "entrypoint": "cli",
                    "cwd": str(cwd),
                    "sessionId": session_id,
                    "version": "2.1.143",
                    "gitBranch": PROJECTS[session["project"]],
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": session["user"]}],
                    },
                },
                {
                    "parentUuid": user_uuid,
                    "isSidechain": False,
                    "type": "assistant",
                    "uuid": _uid(f"claude-asst-{index}"),
                    "timestamp": _iso(offset + 6),
                    "cwd": str(cwd),
                    "sessionId": session_id,
                    "version": "2.1.143",
                    "gitBranch": PROJECTS[session["project"]],
                    "message": {
                        "role": "assistant",
                        "model": session["model"],
                        "content": [{"type": "text", "text": session["reply"]}],
                    },
                },
            ],
        )


# --- Cursor CLI -------------------------------------------------------------

CURSOR_PROMPTS = [
    "convert the deploy script from bash to python",
    "why is the timeout not respected in the worker pool",
    "add type hints to the cache module",
]

CURSOR_SESSION = {
    "project": "telemetry",
    "user": "split the metrics exporter into its own module",
    "reply": (
        "Moved the exporter behind a protocol so the otel and prometheus "
        "backends can share a retry policy."
    ),
}


def seed_cursor(home: pathlib.Path) -> None:
    """Write the Cursor CLI prompt history and one agent transcript.

    Parameters
    ----------
    home : pathlib.Path
        Root of the synthetic demo home.
    """
    _write_json(home / ".config" / "cursor" / "prompt_history.json", CURSOR_PROMPTS)

    project_id = hashlib.sha256(b"telemetry").hexdigest()[:16]
    session_id = _uid("cursor-sess-0")
    cwd = home / "code" / CURSOR_SESSION["project"]
    project_dir = home / ".cursor" / "projects" / project_id
    _write_json(
        project_dir / "repo.json",
        {"repoRoot": str(cwd), "branch": PROJECTS[CURSOR_SESSION["project"]]},
    )
    _write_jsonl(
        project_dir / "agent-transcripts" / session_id / f"{session_id}.jsonl",
        [
            {
                "role": "user",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": (f"<user_query>\n{CURSOR_SESSION['user']}\n</user_query>"),
                        }
                    ]
                },
            },
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": CURSOR_SESSION["reply"],
                            "bubbleId": "bubble-0002",
                        }
                    ]
                },
            },
        ],
    )


# --- Gemini CLI -------------------------------------------------------------

GEMINI_PROMPTS = [
    "summarize the retry semantics used across this codebase",
    "generate a dockerfile for the parser service",
]


def seed_gemini(home: pathlib.Path) -> None:
    """Write one Gemini ``tmp/<project_hash>`` chat plus its prompt log.

    Parameters
    ----------
    home : pathlib.Path
        Root of the synthetic demo home.
    """
    cwd = home / "code" / "parser-lab"
    project_hash = hashlib.sha256(str(cwd).encode("utf-8")).hexdigest()
    tmp = home / ".gemini" / "tmp" / project_hash
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / ".project_root").write_text(str(cwd), encoding="utf-8")

    session_id = _uid("gemini-sess-0")
    _write_jsonl(
        tmp / "chats" / f"session-{_iso(-2 * DAY)[:19].replace(':', '-')}.jsonl",
        [
            {
                "sessionId": session_id,
                "projectHash": project_hash,
                "startTime": _iso(-2 * DAY),
                "lastUpdated": _iso(-2 * DAY),
                "kind": "main",
            },
            {
                "id": _uid("gemini-msg-0"),
                "timestamp": _iso(-2 * DAY + 5),
                "type": "user",
                "content": [{"text": GEMINI_PROMPTS[1]}],
            },
            {
                "id": _uid("gemini-msg-1"),
                "timestamp": _iso(-2 * DAY + 9),
                "type": "gemini",
                "content": (
                    "Here is a multi-stage Dockerfile that builds the parser "
                    "and caches the dependency layer."
                ),
            },
        ],
    )
    _write_json(
        tmp / "logs.json",
        [
            {
                "sessionId": session_id,
                "messageId": index,
                "timestamp": _iso(-2 * DAY + 5 + index),
                "type": "user",
                "message": text,
            }
            for index, text in enumerate(GEMINI_PROMPTS)
        ],
    )


def main(argv: list[str]) -> int:
    """Rebuild the sandbox home at ``argv[1]`` from scratch.

    Parameters
    ----------
    argv : list[str]
        Command-line arguments containing the requested demo home.

    Returns
    -------
    int
        Zero on success or two when the arguments violate the sandbox boundary.
    """
    if len(argv) != 2:
        print("usage: seed_demo_home.py <demo-home>", file=sys.stderr)
        return 2

    home = pathlib.Path(argv[1]).resolve()
    if home != DEMO_HOME:
        print(
            f"refusing to seed demo home outside {DEMO_HOME}: {home}",
            file=sys.stderr,
        )
        return 2

    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)

    for project in PROJECTS:
        (home / "code" / project).mkdir(parents=True, exist_ok=True)

    seed_codex(home)
    seed_claude(home)
    seed_cursor(home)
    seed_gemini(home)

    files = sum(1 for path in home.rglob("*") if path.is_file())
    print(f"seeded {home} ({files} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
