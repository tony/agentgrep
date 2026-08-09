# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "pydantic>=2.11.3",
#     "rich>=13.0",
#     "tomli-w>=1.0",
# ]
# ///
"""Record what an agent's on-disk stores actually look like, per app version.

The store catalogue stamps every descriptor with a scalar ``observed_version``
and ``observed_at``. Those two fields say *when someone last looked*; they do
not say *what was seen*, so bumping a stamp destroys the previous observation
and drift stays invisible until a human re-audits by hand.

This script writes the observation down instead. One TOML manifest per
``(agent, app version)`` pair records, for every catalogued store, whether it
was present, how many sources discovery found, which record discriminator the
store uses, the key set observed for each discriminator value, and the table
and column names of any SQLite file. It also records the paths under the agent
home that no ``store_id`` claims, which is the drift signal itself.

Two manifests for the same agent at different versions diff mechanically, so
"did Grok stop writing ``timestamp``?" is answered by a text diff rather than
by re-reading the store.

Privacy
-------
Manifests carry **schema only**: key names, discriminator values, table and
column names, counts, and ``${HOME}``-tokenised path patterns. They never
carry record values, prompt text, credentials, or local absolute paths.
:func:`_tokenize_path` enforces the path rule and :func:`_sample_key_sets`
reads key names without retaining any value.

Examples
--------
Observe one agent and write its manifest::

    uv run scripts/observe_stores.py observe --agent grok

Observe every installed agent::

    uv run scripts/observe_stores.py observe --agent all

Diff live disk against the newest recorded manifest::

    uv run scripts/observe_stores.py check --agent all
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tomllib
import typing as t

import rich.console
import rich.table

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agentgrep import run_find_query  # noqa: E402  (standalone script bootstraps src/ above)
from agentgrep.records import AgentName  # noqa: E402  (standalone script bootstraps src/ above)
from agentgrep.store_catalog import CATALOG  # noqa: E402  (standalone script bootstraps src/ above)

MANIFEST_VERSION = 1
"""Schema version of the emitted TOML manifest.

Bump when a field changes meaning or is removed, so a reader can tell an old
manifest from a new one without guessing.
"""

OBSERVATIONS_ROOT = REPO_ROOT / "docs" / "_observations"

SAMPLE_FILE_LIMIT = 40
"""Number of source files sampled per store when collecting key sets.

Key-set collection is the expensive part of an observation: it opens files.
Sampling the newest N sources keeps a full-machine run bounded while still
catching a discriminator that only recent app versions write.
"""

SAMPLE_LINE_LIMIT = 400
"""Number of JSONL lines read per sampled file."""

UNCLAIMED_DEPTH = 2
"""Directory depth walked under an agent home when reconciling unclaimed paths."""

_UNCLAIMED_NOISE = re.compile(
    r"""
    (\.lock$)              # advisory locks, recreated every run
    | (\.(bak|backup|orig|tmp)([.-].*)?$)   # editor and tooling backups
    | (\.bak\.)            # scripts/mcp_swap.py writes config.toml.bak.<tag>
    | (-(wal|shm)$)        # SQLite sidecars of an already-claimed database
    | (^\.DS_Store$)
    """,
    re.VERBOSE,
)
"""Entries that are never a real coverage gap.

An unclaimed list is only useful if a human reads it, so churn that no
catalogue row would ever want — lock files, tooling backups, SQLite write-ahead
sidecars — is filtered out. This repo's own ``scripts/mcp_swap.py`` writes
``config.toml.bak.mcp-swap-<stamp>`` into several agent homes, which would
otherwise dominate the list.
"""

_VERSION_RE = re.compile(r"\d+\.\d+[.\w-]*")


@dataclasses.dataclass(frozen=True)
class AgentProbe:
    """How to identify one agent's installed version and home directory.

    Attributes
    ----------
    agent : str
        agentgrep agent id, matching :class:`~agentgrep.records.AgentName`.
    command : tuple[str, ...]
        Argv used to read the installed version. Read-only by contract: only
        ``--version``-style invocations belong here, never a subcommand that
        can mutate agent state.
    homes : tuple[str, ...]
        ``${HOME}``-relative directories that hold the agent's data, walked
        when reconciling unclaimed paths.
    version_file : str
        ``${HOME}``-relative JSON file carrying a version, used when the CLI
        is absent. ``""`` when the agent ships none.
    version_key : str
        Top-level key read from ``version_file``.
    """

    agent: str
    command: tuple[str, ...] = ()
    homes: tuple[str, ...] = ()
    version_file: str = ""
    version_key: str = "version"


AGENT_PROBES: tuple[AgentProbe, ...] = (
    AgentProbe("claude", ("claude", "--version"), (".claude",)),
    AgentProbe("codex", ("codex", "--version"), (".codex",)),
    AgentProbe("cursor-cli", ("cursor-agent", "--version"), (".cursor", ".config/cursor")),
    AgentProbe("cursor-ide", (), (".cursor-server",)),
    AgentProbe("gemini", ("gemini", "--version"), (".gemini",)),
    AgentProbe("antigravity-cli", ("agy", "--version"), (".gemini/antigravity-cli",)),
    AgentProbe("antigravity-ide", (), (".gemini/antigravity",)),
    AgentProbe(
        "grok",
        ("grok", "--version"),
        (".grok",),
        version_file=".grok/version.json",
    ),
    AgentProbe("pi", ("pi", "--version"), (".pi",)),
    AgentProbe(
        "opencode", ("opencode", "--version"), (".local/share/opencode", ".config/opencode")
    ),
    AgentProbe("vscode", ("code", "--version"), (".config/Code",)),
    AgentProbe("windsurf", (), (".codeium/windsurf",)),
)
"""Per-agent version and home probes, in catalogue order."""


def _tokenize_path(path: pathlib.Path | str) -> str:
    """Replace the real home directory with a ``${HOME}`` token.

    Manifests are committed, so a local absolute path in one is a privacy leak
    and a portability bug at once. Every path that reaches a manifest passes
    through here.

    Parameters
    ----------
    path : pathlib.Path or str
        Path to tokenize.

    Returns
    -------
    str
        Path text with the home prefix replaced by ``${HOME}``.

    Examples
    --------
    >>> import os, pathlib
    >>> home = pathlib.Path(os.path.expanduser("~"))
    >>> _tokenize_path(home / ".grok" / "sessions")
    '${HOME}/.grok/sessions'
    >>> _tokenize_path("/etc/hosts")
    '/etc/hosts'
    """
    text = str(path)
    home = str(pathlib.Path.home())
    if text == home:
        return "${HOME}"
    if text.startswith(home + os.sep):
        return "${HOME}/" + text[len(home) + 1 :].replace(os.sep, "/")
    return text.replace(os.sep, "/")


def _read_installed_version(probe: AgentProbe) -> tuple[str, str]:
    """Read an agent's installed version without mutating its state.

    Tries the CLI first because it is authoritative, then a shipped version
    file, then gives up. A missing agent is a normal answer, not an error: a
    manifest is only written for what this machine actually has.

    Parameters
    ----------
    probe : AgentProbe
        Agent probe describing the command and fallback file.

    Returns
    -------
    tuple[str, str]
        ``(version, source)`` where source is ``cli``, ``version_file``, or
        ``unknown``.
    """
    if probe.command:
        try:
            completed = subprocess.run(
                probe.command,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except OSError, subprocess.SubprocessError:
            completed = None
        if completed is not None and completed.returncode == 0:
            match = _VERSION_RE.search(completed.stdout)
            if match is not None:
                return match.group(0), "cli"

    if probe.version_file:
        candidate = pathlib.Path.home() / probe.version_file
        if candidate.is_file():
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except OSError, ValueError:
                payload = None
            if isinstance(payload, dict):
                value = payload.get(probe.version_key)
                if isinstance(value, str) and value:
                    return value, "version_file"

    return "", "unknown"


def _discovered_sources(agent: str) -> dict[str, list[pathlib.Path]]:
    """Group discovered source paths by catalogue ``store_id``.

    Uses agentgrep's own discovery rather than a private walk, so the manifest
    records what the shipped code actually finds. A store the catalogue names
    but discovery never reaches therefore shows ``present = false`` — which is
    exactly the gap worth recording.

    Parameters
    ----------
    agent : str
        agentgrep agent id.

    Returns
    -------
    dict[str, list[pathlib.Path]]
        Mapping of store id to discovered paths, newest first.
    """
    try:
        records = run_find_query(
            pathlib.Path.home(),
            (t.cast("AgentName", agent),),
            pattern=None,
            limit=None,
        )
    except ValueError:
        return {}

    grouped: dict[str, list[pathlib.Path]] = collections.defaultdict(list)
    for record in records:
        if record.store:
            grouped[record.store].append(record.path)
    for paths in grouped.values():
        paths.sort(key=_mtime, reverse=True)
    return dict(grouped)


def _mtime(path: pathlib.Path) -> float:
    """Return a path's modification time, or ``0.0`` when it cannot be read.

    Discovery can name a source that vanishes before the observation reads it
    — a live agent rotates logs mid-run — so a missing file sorts last rather
    than aborting the whole manifest.

    Parameters
    ----------
    path : pathlib.Path
        Path to stat.

    Returns
    -------
    float
        Unix mtime, or ``0.0``.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _iter_json_records(path: pathlib.Path) -> t.Iterator[dict[str, object]]:
    """Yield mapping records from a JSON or JSONL source.

    Values are never retained by callers; only key names leave this function.

    Parameters
    ----------
    path : pathlib.Path
        Source file to read.

    Yields
    ------
    dict[str, object]
        One decoded mapping per record.
    """
    suffix = path.suffix.lower()
    try:
        if suffix == ".jsonl":
            with path.open(encoding="utf-8", errors="replace") as handle:
                for index, line in enumerate(handle):
                    if index >= SAMPLE_LINE_LIMIT:
                        return
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        payload = json.loads(stripped)
                    except ValueError:
                        continue
                    if isinstance(payload, dict):
                        yield t.cast("dict[str, object]", payload)
            return
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except OSError, ValueError, RecursionError:
        return
    if isinstance(payload, dict):
        yield t.cast("dict[str, object]", payload)
    elif isinstance(payload, list):
        for item in t.cast("list[object]", payload)[:SAMPLE_LINE_LIMIT]:
            if isinstance(item, dict):
                yield t.cast("dict[str, object]", item)


def _discriminator_key(records: list[dict[str, object]]) -> str:
    """Pick the key that discriminates record kinds within a store.

    Agents disagree on the name — Codex and Claude use ``type``, Pi and
    OpenCode use ``role``, Antigravity CLI uses ``type`` with a different
    vocabulary — so the key is inferred rather than assumed: the first
    candidate present on most records with more than one distinct string value.

    Parameters
    ----------
    records : list[dict[str, object]]
        Sampled records.

    Returns
    -------
    str
        Discriminator key name, or ``""`` when the store has no discriminator.

    Examples
    --------
    >>> _discriminator_key([{"type": "user"}, {"type": "assistant"}])
    'type'
    >>> _discriminator_key([{"role": "user"}, {"role": "tool"}])
    'role'
    >>> _discriminator_key([{"text": "a"}, {"text": "b"}])
    ''
    """
    for candidate in ("type", "role", "kind", "sessionUpdate"):
        values = {record[candidate] for record in records if isinstance(record.get(candidate), str)}
        if len(values) > 1:
            return candidate
    for candidate in ("type", "role", "kind"):
        if any(isinstance(record.get(candidate), str) for record in records):
            return candidate
    return ""


def _sample_key_sets(paths: list[pathlib.Path]) -> tuple[str, dict[str, list[str]], int]:
    """Collect per-discriminator key sets from a store's sources.

    Only key *names* are retained. Values are inspected solely to read the
    discriminator, which is itself a schema token rather than user content.

    Parameters
    ----------
    paths : list[pathlib.Path]
        Discovered source paths, newest first.

    Returns
    -------
    tuple[str, dict[str, list[str]], int]
        ``(discriminator_key, {discriminator_value: sorted key names}, records_sampled)``.
    """
    records: list[dict[str, object]] = []
    for path in paths[:SAMPLE_FILE_LIMIT]:
        if path.suffix.lower() not in {".json", ".jsonl"}:
            continue
        records.extend(_iter_json_records(path))

    if not records:
        return "", {}, 0

    discriminator = _discriminator_key(records)
    grouped: dict[str, set[str]] = collections.defaultdict(set)
    for record in records:
        value = record.get(discriminator) if discriminator else None
        bucket = value if isinstance(value, str) else "*"
        grouped[bucket].update(record.keys())
    return (
        discriminator,
        {key: sorted(value) for key, value in sorted(grouped.items())},
        len(records),
    )


def _sqlite_schema(path: pathlib.Path) -> dict[str, list[str]]:
    """Read table and column names from a SQLite file, read-only.

    Opened through a ``file:`` URI in ``mode=ro`` so an observation can never
    write, checkpoint, or upgrade a live agent database.

    Parameters
    ----------
    path : pathlib.Path
        SQLite database path.

    Returns
    -------
    dict[str, list[str]]
        Mapping of table name to column names. Empty when unreadable.
    """
    uri = f"file:{path}?mode=ro"
    schema: dict[str, list[str]] = {}
    try:
        with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
            ).fetchall()
            for (name,) in tables:
                if not isinstance(name, str) or name.startswith("sqlite_"):
                    continue
                columns = connection.execute(f"PRAGMA table_info('{name}')").fetchall()
                schema[name] = [str(row[1]) for row in columns]
    except sqlite3.Error:
        return {}
    return schema


def _claimed_prefixes(agent: str) -> list[str]:
    """Return the ``${HOME}``-relative path prefixes the catalogue claims.

    Parameters
    ----------
    agent : str
        agentgrep agent id.

    Returns
    -------
    list[str]
        Tokenized path patterns from every descriptor for this agent.
    """
    return [
        descriptor.path_pattern for descriptor in CATALOG.stores if str(descriptor.agent) == agent
    ]


def _unclaimed_entries(probe: AgentProbe, claimed: list[str]) -> list[dict[str, object]]:
    """List paths under an agent home that no descriptor pattern mentions.

    Matching is deliberately literal and generous: an entry counts as claimed
    when its tokenized path appears as a substring of any descriptor pattern,
    or vice versa. A generous test under-reports rather than crying wolf, and
    an under-report is the safer failure for a signal a human triages.

    Parameters
    ----------
    probe : AgentProbe
        Agent probe naming the home directories to walk.
    claimed : list[str]
        Descriptor path patterns for this agent.

    Returns
    -------
    list[dict[str, object]]
        Sorted unclaimed entries carrying tokenized path and kind.
    """
    claimed_text = "\n".join(claimed)
    # Expand ``{a,b}`` alternation before taking prefixes. Truncating at the
    # brace instead collapses a row like ``.../{settings*.json,keybindings.json}``
    # to the bare agent home, which then claims every entry underneath it and
    # silently reports zero gaps for the agent with the most stores.
    # A prefix is usable only if it reaches *inside* an agent home. Two kinds of
    # row otherwise produce one that swallows everything: a brace row whose
    # widest alternative truncates to the home itself, and a project-local row
    # like "${HOME}/<known_project_root>/.claude/..." that truncates to
    # "${HOME}/" and would claim every file the user owns.
    roots = {_tokenize_path(pathlib.Path.home() / home).rstrip("/") for home in probe.homes}
    prefixes = [
        prefix
        for pattern in claimed
        for expanded in _expand_braces(pattern)
        if (prefix := _strip_pattern(expanded)) != "\x00"
        and any(prefix.startswith(f"{root}/") and prefix.rstrip("/") != root for root in roots)
    ]

    entries: list[dict[str, object]] = []
    for home in probe.homes:
        root = pathlib.Path.home() / home
        if not root.is_dir():
            continue
        for depth in range(1, UNCLAIMED_DEPTH + 1):
            pattern = "/".join(["*"] * depth)
            for candidate in sorted(root.glob(pattern)):
                token = _tokenize_path(candidate)
                leaf = candidate.name
                if _UNCLAIMED_NOISE.search(leaf):
                    continue
                if leaf in claimed_text or token in claimed_text:
                    continue
                if any(token.startswith(prefix) for prefix in prefixes):
                    continue
                entries.append(
                    {
                        "path_pattern": token,
                        "kind": "dir" if candidate.is_dir() else "file",
                    }
                )
    seen: set[str] = set()
    unique: list[dict[str, object]] = []
    for entry in entries:
        key = str(entry["path_pattern"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


_ENV_HEAD_RE = re.compile(r"\$\{[A-Z_]+ or (\$\{HOME\}[^}]*)\}")
_BRACE_GROUP_RE = re.compile(r"(?<!\$)\{([^{}]*)\}")
_DATE_PLACEHOLDER_RE = re.compile(r"(?<=/)(?:YYYY|MM|DD)(?=/)")
"""A date placeholder occupying a whole path segment.

Anchored to segment boundaries on purpose: a bare ``ss`` alternative matches
inside ``sessions`` and truncates the prefix mid-word.
"""


def _expand_braces(pattern: str) -> list[str]:
    """Expand one ``{a,b}`` alternation group into separate patterns.

    Descriptor rows fold sibling files into one row with brace alternation.
    Each alternative is a distinct claim, so the prefix test has to see them
    individually. The ``${HOME}`` substitution token is left alone via the
    negative lookbehind on ``$``.

    Parameters
    ----------
    pattern : str
        Descriptor path pattern.

    Returns
    -------
    list[str]
        One pattern per alternative, or the input unchanged when it has none.

    Examples
    --------
    >>> _expand_braces("${HOME}/.codex/{config.toml,*.toml}")
    ['${HOME}/.codex/config.toml', '${HOME}/.codex/*.toml']
    >>> _expand_braces("${HOME}/.claude/history.jsonl")
    ['${HOME}/.claude/history.jsonl']
    """
    match = _BRACE_GROUP_RE.search(pattern)
    if match is None:
        return [pattern]
    head, tail = pattern[: match.start()], pattern[match.end() :]
    return [f"{head}{option}{tail}" for option in match.group(1).split(",")]


def _strip_pattern(pattern: str) -> str:
    r"""Return the literal ``${HOME}``-rooted prefix of a descriptor pattern.

    Descriptor patterns carry an ``${ENV or ${HOME}/x}`` head that names the
    environment override before the default root, then a ``<token>`` or glob
    tail. Only the literal head is usable as a prefix test, and the override
    form has to be reduced to its default root first — most Claude, Codex and
    Grok rows use it, so failing to reduce it makes every one of their stores
    look unclaimed.

    Parameters
    ----------
    pattern : str
        Descriptor ``path_pattern``.

    Returns
    -------
    str
        Literal ``${HOME}``-rooted prefix, or a sentinel matching nothing.

    Examples
    --------
    >>> _strip_pattern("${HOME}/.grok/sessions/<project>/x.jsonl")
    '${HOME}/.grok/sessions/'
    >>> _strip_pattern("${GROK_HOME or ${HOME}/.grok}/logs/unified.jsonl")
    '${HOME}/.grok/logs/unified.jsonl'
    >>> _strip_pattern("%APPDATA%/Code/User/globalStorage/state.vscdb")
    '\x00'
    """
    pattern = _ENV_HEAD_RE.sub(r"\1", pattern)
    # Codex spells its date-sharded path with literal YYYY/MM/DD placeholders
    # rather than <tokens>, so those are a third kind of wildcard to stop at.
    date_stop = _DATE_PLACEHOLDER_RE.search(pattern)
    stops = [
        pattern.find("<"),
        pattern.find("*"),
        _brace_group_index(pattern),
        date_stop.start() if date_stop else -1,
    ]
    cuts = [index for index in stops if index != -1]
    if cuts:
        pattern = pattern[: min(cuts)]
    if not pattern.startswith("${HOME}"):
        return "\x00"
    return pattern


def _brace_group_index(pattern: str) -> int:
    """Return the index of the first brace *glob* group, ignoring ``${...}``.

    A descriptor pattern uses braces for two unrelated things: the ``${HOME}``
    and ``${ENV}`` substitution tokens, and ``{json,jsonc}`` alternation globs.
    Only the second terminates the literal prefix, so a naive search for ``{``
    truncates the pattern to ``"$"`` and makes every store look unclaimed.

    Parameters
    ----------
    pattern : str
        Descriptor path pattern.

    Returns
    -------
    int
        Index of the first glob brace, or ``-1``.

    Examples
    --------
    >>> _brace_group_index("${HOME}/.codex/config.{toml,json}")
    22
    >>> _brace_group_index("${HOME}/.claude/history.jsonl")
    -1
    """
    for index, char in enumerate(pattern):
        if char == "{" and (index == 0 or pattern[index - 1] != "$"):
            return index
    return -1


def observe_agent(probe: AgentProbe) -> dict[str, object] | None:
    """Build one agent's manifest payload from live disk.

    Parameters
    ----------
    probe : AgentProbe
        Agent probe to observe.

    Returns
    -------
    dict[str, object] or None
        Manifest payload ready for TOML emission, or ``None`` when the agent
        has neither a readable version nor any store on this machine.
    """
    version, version_source = _read_installed_version(probe)
    discovered = _discovered_sources(probe.agent)
    descriptors = [d for d in CATALOG.stores if str(d.agent) == probe.agent]
    on_disk = any((pathlib.Path.home() / home).exists() for home in probe.homes)
    if not version and not discovered and not on_disk:
        return None

    stores: list[dict[str, object]] = []
    for descriptor in descriptors:
        # A descriptor's ``store_id`` is the catalogue name; discovery emits an
        # underscore-flattened runtime key declared on each DiscoverySpec. They
        # are different namespaces (``gemini.tmp.chats`` vs ``gemini.tmp_chats``),
        # so the join has to go through the specs or every such store reads as
        # absent.
        runtime_keys = sorted({spec.store for spec in descriptor.discovery})
        paths = [path for key in runtime_keys for path in discovered.get(key, [])]
        entry: dict[str, object] = {
            "id": descriptor.store_id,
            "path_pattern": descriptor.path_pattern,
            "role": str(descriptor.role),
            "format": str(descriptor.format),
            # Distinguish "the catalogue never tries" from "the catalogue tried
            # and found nothing". Only the second is drift; the first is the
            # coverage decision recorded in the row itself.
            "has_discovery": bool(runtime_keys),
            "source_count": len(paths),
        }
        if runtime_keys:
            entry["discovers_as"] = runtime_keys
        if paths:
            discriminator, key_sets, sampled = _sample_key_sets(paths)
            if discriminator:
                entry["discriminator"] = discriminator
            if key_sets:
                entry["records_sampled"] = sampled
                entry["record_keys"] = key_sets
            sqlite_paths = [p for p in paths if p.suffix.lower() in {".db", ".sqlite", ".vscdb"}]
            if sqlite_paths:
                schema = _sqlite_schema(sqlite_paths[0])
                if schema:
                    entry["tables"] = schema
        stores.append(entry)

    return {
        "manifest_version": MANIFEST_VERSION,
        "agent": {
            "id": probe.agent,
            "app_version": version or "unknown",
            "version_source": version_source,
        },
        "observation": {
            # Local date, not UTC: an observation is stamped with the day the
            # operator ran it, and a UTC date reads a day ahead for evening
            # runs in western timezones.
            "observed_at": datetime.datetime.now().astimezone().date(),
            "platform": sys.platform,
            "catalog_version": CATALOG.catalog_version,
            "generator": "scripts/observe_stores.py",
        },
        "store": stores,
        "unclaimed": _unclaimed_entries(probe, _claimed_prefixes(probe.agent)),
    }


def manifest_path(agent: str, version: str) -> pathlib.Path:
    """Return the on-disk location of one agent-version manifest.

    Parameters
    ----------
    agent : str
        agentgrep agent id.
    version : str
        Installed app version.

    Returns
    -------
    pathlib.Path
        ``observations/<agent>/<version>.toml``.

    Examples
    --------
    >>> manifest_path("grok", "1.0.0").name
    '1.0.0.toml'
    >>> manifest_path("grok", "2026.08.04-aaa8809").name
    '2026.08.04-aaa8809.toml'
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", version or "unknown")
    return OBSERVATIONS_ROOT / agent / f"{safe}.toml"


def _would_shadow(agent: str, app_version: str) -> bool:
    """Whether writing this observation would hide a versioned manifest.

    A run that cannot read the CLI version records ``unknown`` and stamps it
    with today's date, so it becomes the newest manifest and hides the real
    one. Readers select by date, so nothing downstream would notice.
    """
    if app_version != "unknown":
        return False
    directory = OBSERVATIONS_ROOT / agent
    return any(path.stem != "unknown" for path in directory.glob("*.toml"))


def write_manifest(payload: dict[str, object]) -> pathlib.Path:
    """Write one manifest to ``observations/`` and return its path.

    Parameters
    ----------
    payload : dict[str, object]
        Manifest payload from :func:`observe_agent`.

    Returns
    -------
    pathlib.Path
        Path written.
    """
    agent = t.cast("dict[str, str]", payload["agent"])
    target = manifest_path(agent["id"], agent["app_version"])
    import tomli_w  # only the writer needs it; check/read paths do not

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as handle:
        tomli_w.dump(payload, handle, multiline_strings=False)
    return target


def newest_manifest(agent: str) -> pathlib.Path | None:
    """Return the most recently observed manifest for one agent.

    Parameters
    ----------
    agent : str
        agentgrep agent id.

    Returns
    -------
    pathlib.Path or None
        Newest manifest by ``observed_at``, or ``None`` when none exists.
        Dates have day granularity, so the file name breaks a same-day tie.
    """
    directory = OBSERVATIONS_ROOT / agent
    if not directory.is_dir():
        return None
    best: tuple[datetime.date, str, pathlib.Path] | None = None
    for candidate in sorted(directory.glob("*.toml")):
        try:
            payload = tomllib.loads(candidate.read_text(encoding="utf-8"))
        except OSError, tomllib.TOMLDecodeError:
            continue
        observed = payload.get("observation", {}).get("observed_at")
        if not isinstance(observed, datetime.date):
            continue
        key = (observed, candidate.name)
        if best is None or key > best[:2]:
            best = (observed, candidate.name, candidate)
    return None if best is None else best[2]


def diff_manifest(previous: dict[str, object], current: dict[str, object]) -> list[str]:
    """Describe how a stored manifest differs from a fresh observation.

    Parameters
    ----------
    previous : dict[str, object]
        Manifest loaded from ``observations/``.
    current : dict[str, object]
        Freshly observed manifest payload.

    Returns
    -------
    list[str]
        Human-readable drift lines, empty when the two agree.
    """
    lines: list[str] = []
    old_agent = t.cast("dict[str, str]", previous.get("agent", {}))
    new_agent = t.cast("dict[str, str]", current.get("agent", {}))
    if old_agent.get("app_version") != new_agent.get("app_version"):
        lines.append(
            f"app version {old_agent.get('app_version')} -> {new_agent.get('app_version')}"
        )

    old_stores = {
        str(item["id"]): item
        for item in t.cast("list[dict[str, object]]", previous.get("store", []))
    }
    new_stores = {
        str(item["id"]): item
        for item in t.cast("list[dict[str, object]]", current.get("store", []))
    }
    for store_id in sorted(set(old_stores) | set(new_stores)):
        old = old_stores.get(store_id)
        new = new_stores.get(store_id)
        if old is None:
            lines.append(f"{store_id}: new store row")
            continue
        if new is None:
            lines.append(f"{store_id}: store row removed from catalogue")
            continue
        old_disc = old.get("has_discovery")
        new_disc = new.get("has_discovery")
        if bool(old_disc) != bool(new_disc):
            lines.append(f"{store_id}: has_discovery {old_disc} -> {new_disc}")
        old_count = int(t.cast("int", old.get("source_count", 0)))
        new_count = int(t.cast("int", new.get("source_count", 0)))
        if (old_count == 0) != (new_count == 0):
            lines.append(f"{store_id}: source_count {old_count} -> {new_count}")
        old_key = old.get("discriminator")
        new_key = new.get("discriminator")
        if old_key != new_key:
            lines.append(f"{store_id}: discriminator {old_key!r} -> {new_key!r}")
        old_keys = t.cast("dict[str, list[str]]", old.get("record_keys", {}))
        new_keys = t.cast("dict[str, list[str]]", new.get("record_keys", {}))
        for value in sorted(set(old_keys) | set(new_keys)):
            gone = sorted(set(old_keys.get(value, [])) - set(new_keys.get(value, [])))
            added = sorted(set(new_keys.get(value, [])) - set(old_keys.get(value, [])))
            if gone:
                lines.append(f"{store_id}[{value}]: keys removed {gone}")
            if added:
                lines.append(f"{store_id}[{value}]: keys added {added}")
    return lines


def _selected_probes(agent: str) -> list[AgentProbe]:
    """Resolve the ``--agent`` argument to probes."""
    if agent == "all":
        return list(AGENT_PROBES)
    return [probe for probe in AGENT_PROBES if probe.agent == agent]


def main(argv: list[str] | None = None) -> int:
    """Run the observation CLI.

    Parameters
    ----------
    argv : list[str] or None
        Argument vector, defaulting to :data:`sys.argv`.

    Returns
    -------
    int
        ``0`` on success, ``1`` when ``check`` found drift.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=("observe", "check"))
    choices = ["all", *(probe.agent for probe in AGENT_PROBES)]
    parser.add_argument("--agent", default="all", choices=choices)
    args = parser.parse_args(argv)

    console = rich.console.Console()
    drifted = False

    for probe in _selected_probes(args.agent):
        payload = observe_agent(probe)
        if payload is None:
            console.print(f"[dim]{probe.agent}: not installed, skipped[/dim]")
            continue

        agent_meta = t.cast("dict[str, str]", payload["agent"])
        if args.command == "observe":
            if _would_shadow(probe.agent, agent_meta["app_version"]):
                console.print(
                    f"[yellow]{probe.agent}: version unreadable; refusing to write "
                    f"unknown.toml over a versioned manifest[/yellow]"
                )
                drifted = True
                continue
            target = write_manifest(payload)
            console.print(
                f"{probe.agent} {agent_meta['app_version']} -> {target.relative_to(REPO_ROOT)}"
            )
            continue

        stored = newest_manifest(probe.agent)
        if stored is None:
            console.print(f"[yellow]{probe.agent}: no manifest recorded[/yellow]")
            drifted = True
            continue
        previous = tomllib.loads(stored.read_text(encoding="utf-8"))
        lines = diff_manifest(previous, payload)
        if not lines:
            console.print(f"[green]{probe.agent}: matches {stored.name}[/green]")
            continue
        drifted = True
        table = rich.table.Table(title=f"{probe.agent}: drift vs {stored.name}")
        table.add_column("change")
        for line in lines:
            table.add_row(line)
        console.print(table)

    return 1 if drifted else 0


if __name__ == "__main__":
    raise SystemExit(main())
