"""Helpers for project-origin metadata.

Project origin is optional record metadata. It is deliberately kept out of the
plain text haystack so origin filters do not change ordinary search relevance
or source prefilter behavior.

This module is also the shared seam for *recovering* origin from the names a
store chose: :func:`decode_project_dir` decodes a project directory a store
hid in a path segment, and :func:`origin_cwd_hash` decides whether a segment
is a digest at all. The mechanism is shared; the vocabulary is not — each
adapter still names the keys and segments its own store writes.
"""

from __future__ import annotations

import dataclasses
import enum
import fnmatch
import os
import pathlib
import re
import typing as t
import urllib.parse

from agentgrep.records import RecordOrigin, SearchRecord, SourceOriginSummary

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from agentgrep.query.ast import FieldEqNode

__all__ = [
    "CWD_DIGEST_LENGTH",
    "DASH_DECODE_MAX_TOKENS",
    "DASH_DECODE_PROBE_BUDGET",
    "LEGACY_ORIGIN_METADATA_KEYS",
    "ORIGIN_PATH_QUERY_FIELDS",
    "ORIGIN_QUERY_FIELDS",
    "ORIGIN_STRING_QUERY_FIELDS",
    "PRUNABLE_ORIGIN_FIELDS",
    "OriginEncoding",
    "ProjectDirCache",
    "_origin_path_boundary_text",
    "_path_is_equal_or_descendant",
    "decode_project_dir",
    "is_cwd_digest",
    "is_path_like_text",
    "normalize_origin_path_text",
    "origin_cwd_hash",
    "origin_filter_nodes",
    "record_matches_origin",
    "record_origin_field_values",
]

PRUNABLE_ORIGIN_FIELDS: frozenset[str] = frozenset({"cwd_hash"})
"""Origin fields a source-level summary may claim as complete.

:attr:`~agentgrep.records.SourceOriginSummary.complete_fields` is a claim that
:mod:`agentgrep.query.evaluate` acts on by *dropping whole sources before the
first byte is read*. It is therefore a claim about **values**, not about names:
a source that claims ``cwd`` completeness while any of its records carries a
different ``cwd`` deletes its own matching record.

``cwd`` never survives that test. It is discovered from a sibling file or a
path segment, but the parser can learn a *different* working directory from
inside the payload — a Cursor ``composerData`` bubble carries its own ``cwd``,
and a Gemini session can name a directory other than the project root. Only
``cwd_hash`` is a property of the source's own location, so only ``cwd_hash``
is prunable.
"""

CWD_DIGEST_LENGTH: int = 32
"""Hex length of the digest Cursor derives from a workspace path (md5).

Stores that use a different digest width pass their own ``length`` to
:func:`is_cwd_digest`.
"""

_HEX_DIGEST_RE = re.compile(r"[0-9a-f]+")

DASH_DECODE_MAX_TOKENS: int = 32
"""Dash-separated tokens considered for one reconstruction."""

DASH_DECODE_PROBE_BUDGET: int = 512
"""Directory probes one dash reconstruction may spend before giving up.

The reconstruction is filesystem-directed, so a pathological name could
otherwise fan out combinatorially. Exhausting the budget yields ``None`` — a
known-unknown, never a guess.
"""


class OriginEncoding(enum.StrEnum):
    """How a store encoded a working directory into a name it chose.

    ``URL`` is lossless: percent-decoding recovers the exact path. ``DASH`` is
    **lossy** — every separator became ``-`` and nothing was escaped, so the
    encoded name alone cannot say which dashes were separators.
    """

    URL = "url"
    DASH = "dash"


type ProjectDirCache = cabc.MutableMapping[str, str | None]
"""Memo for :func:`decode_project_dir`, owned by the caller.

Dash decoding probes the filesystem, and one project directory commonly backs
many transcripts, so the decode wants a memo. It must not be a module-level
:func:`functools.cache`: the TUI and the MCP server are long-lived processes,
and a process-lifetime memo would keep answering from a directory layout that
has since changed. The cache is therefore passed in and scoped to the search
runtime that owns it.
"""

LEGACY_ORIGIN_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "branch",
        "cwd",
        "cwd_hash",
        "directory",
        "gitBranch",
        "project",
        "project_hash",
        "projectHash",
        "repo",
        "repository",
        "workspace",
        "worktree",
    },
)

_PATH_FIELD_KEYS: dict[str, tuple[str, ...]] = {
    "cwd": ("cwd", "project", "workspace", "directory"),
    "repo": ("repo", "repository", "worktree", "cwd", "workspace", "directory"),
    "worktree": ("worktree", "workspace", "directory"),
}
_STRING_FIELD_KEYS: dict[str, tuple[str, ...]] = {
    "branch": ("branch", "gitBranch"),
    "cwd_hash": ("cwd_hash", "project_hash", "projectHash"),
}

# Query-field names backed by record origin. query.evaluate dispatches on
# these; a registered origin field missing here would silently never match.
ORIGIN_PATH_QUERY_FIELDS: frozenset[str] = frozenset(_PATH_FIELD_KEYS)
ORIGIN_STRING_QUERY_FIELDS: frozenset[str] = frozenset(_STRING_FIELD_KEYS) | {"project"}
ORIGIN_QUERY_FIELDS: frozenset[str] = ORIGIN_PATH_QUERY_FIELDS | ORIGIN_STRING_QUERY_FIELDS

_OriginMatchKind = t.Literal["path", "string"]
_OriginSummaryState = t.Literal["T", "F", "U"]

_CONTEXT_FIELD_VALUES: dict[str, tuple[str, ...]] = {
    "branch": ("branch",),
    "cwd": ("cwd",),
    "repo": ("repo", "worktree", "cwd"),
    "worktree": ("worktree", "cwd"),
    "cwd_hash": ("cwd_hash",),
}


@dataclasses.dataclass(slots=True, frozen=True)
class OriginPredicate:
    """One compiled origin predicate against a record's origin fields."""

    fields: tuple[str, ...]
    value: str
    kind: _OriginMatchKind
    variants: tuple[str, ...]
    is_glob: bool

    @classmethod
    def from_field_value(
        cls,
        field: str,
        value: str,
        *,
        fields: tuple[str, ...] | None = None,
        variants: t.Iterable[str] | None = None,
        is_glob: bool | None = None,
    ) -> OriginPredicate | None:
        """Compile one origin field/value predicate."""
        if field in ORIGIN_PATH_QUERY_FIELDS:
            compiled_variants = tuple(variants) if variants is not None else (value,)
            return cls(
                fields=(field,) if fields is None else fields,
                value=value,
                kind="path",
                variants=compiled_variants,
                is_glob=_origin_path_is_glob(compiled_variants) if is_glob is None else is_glob,
            )
        if field in ORIGIN_STRING_QUERY_FIELDS:
            return cls(
                fields=(field,) if fields is None else fields,
                value=value,
                kind="string",
                variants=(value,),
                is_glob=_origin_string_is_glob(value) if is_glob is None else is_glob,
            )
        return None

    def matches(self, record: SearchRecord) -> bool:
        """Return whether ``record`` satisfies this origin predicate."""
        values = _dedupe(
            value for field in self.fields for value in record_origin_field_values(record, field)
        )
        if self.kind == "path":
            return any(self._path_matches(value) for value in values)
        return any(
            _origin_string_equal(value, self.value, is_glob=self.is_glob) for value in values
        )

    def evaluate_summary(self, summary: SourceOriginSummary | None) -> _OriginSummaryState:
        """Evaluate this predicate against complete source-origin facts."""
        if summary is None or any(field not in summary.complete_fields for field in self.fields):
            return "U"
        if not summary.origins:
            return "F"
        matches = [self._matches_origin(origin) for origin in summary.origins]
        if all(matches):
            return "T"
        if any(matches):
            return "U"
        return "F"

    def _matches_origin(self, origin: RecordOrigin) -> bool:
        values = _dedupe(
            value for field in self.fields for value in origin_field_values(origin, field)
        )
        if self.kind == "path":
            return any(self._path_matches(value) for value in values)
        return any(
            _origin_string_equal(value, self.value, is_glob=self.is_glob) for value in values
        )

    def _path_matches(self, value: str) -> bool:
        if self.is_glob:
            return any(fnmatch.fnmatchcase(value, variant) for variant in self.variants)
        path = _origin_path_boundary_text(value)
        return any(
            _path_is_equal_or_descendant(path, _origin_path_boundary_text(variant))
            for variant in self.variants
        )


@dataclasses.dataclass(slots=True, frozen=True)
class OriginMatcher:
    """Compiled matcher for origin fields and origin-context boosts."""

    predicates: tuple[OriginPredicate, ...]

    @classmethod
    def from_field_value(
        cls,
        field: str,
        value: str,
        *,
        variants: t.Iterable[str] | None = None,
        is_glob: bool | None = None,
    ) -> OriginMatcher:
        """Compile one origin query field/value predicate."""
        predicate = OriginPredicate.from_field_value(
            field,
            value,
            variants=variants,
            is_glob=is_glob,
        )
        return cls(()) if predicate is None else cls((predicate,))

    @classmethod
    def from_origin(cls, origin: RecordOrigin | None) -> OriginMatcher:
        """Compile a :class:`RecordOrigin` into same-context predicates."""
        if origin is None or origin.is_empty():
            return cls(())
        predicates: list[OriginPredicate] = []
        for field in ("branch", "cwd", "repo", "worktree", "cwd_hash"):
            value = t.cast("str | None", getattr(origin, field))
            if not value:
                continue
            predicate = OriginPredicate.from_field_value(
                field,
                value,
                fields=_CONTEXT_FIELD_VALUES[field],
            )
            if predicate is not None:
                predicates.append(predicate)
        return cls(tuple(predicates))

    def matches(self, record: SearchRecord) -> bool:
        """Return whether ``record`` satisfies every compiled predicate."""
        return bool(self.predicates) and all(
            predicate.matches(record) for predicate in self.predicates
        )

    def may_match_summary(self, summary: SourceOriginSummary | None) -> bool:
        """Return whether a source summary cannot rule this matcher out."""
        return self.evaluate_summary(summary) != "F"

    def evaluate_summary(self, summary: SourceOriginSummary | None) -> _OriginSummaryState:
        """Evaluate every predicate against source-origin facts."""
        if not self.predicates:
            return "T"
        states = [predicate.evaluate_summary(summary) for predicate in self.predicates]
        if "F" in states:
            return "F"
        if "U" in states:
            return "U"
        return "T"


def is_path_like_text(text: str) -> bool:
    """Return whether a store value looks like a filesystem path.

    Shared by ingest gating (adapters) and display rewriting
    (serializers) so origin fields and legacy metadata agree on what
    counts as a path.

    >>> is_path_like_text("/work/proj"), is_path_like_text("~")
    (True, True)
    >>> is_path_like_text("a1b2-uuid")
    False
    """
    return "/" in text or "\\" in text or text == "~"


def is_cwd_digest(text: str, *, length: int = CWD_DIGEST_LENGTH) -> bool:
    """Return whether a path segment has the shape of a working-directory digest.

    Stores that name a directory after a hash of the working directory sit next
    to directories that are not hashes at all — ``globalStorage``, ``User``, a
    timestamped scratch directory. Without a shape check the sibling's *name*
    becomes a ``cwd_hash``, and a fabricated digest is directly searchable: it
    answers ``cwd_hash:`` predicates with a value no agent ever wrote.

    The check runs one way only. A digest is accepted when the store wrote one;
    a ``cwd_hash`` is never *synthesized* by hashing a recovered ``cwd``, which
    would invent an identity for a directory the store never hashed.

    >>> is_cwd_digest("9b2a1f0c4d3e5a6b7c8d9e0f1a2b3c4d")
    True
    >>> is_cwd_digest("globalStorage")
    False
    >>> is_cwd_digest("31fc949449b1c906", length=16)
    True
    """
    return len(text) == length and _HEX_DIGEST_RE.fullmatch(text) is not None


def origin_cwd_hash(segment: str | None, *, length: int = CWD_DIGEST_LENGTH) -> str | None:
    """Return a path segment as a ``cwd_hash``, or ``None`` when it is not one.

    The accept-guard every store shares before labelling a path segment a
    digest. See :func:`is_cwd_digest` for why the shape check exists.

    >>> origin_cwd_hash("9b2a1f0c4d3e5a6b7c8d9e0f1a2b3c4d")
    '9b2a1f0c4d3e5a6b7c8d9e0f1a2b3c4d'
    >>> origin_cwd_hash(".cursor") is None
    True
    """
    if not segment or not is_cwd_digest(segment, length=length):
        return None
    return segment


def decode_project_dir(
    name: str,
    *,
    encoding: OriginEncoding,
    cache: ProjectDirCache | None = None,
) -> str | None:
    """Decode the working directory a store hid in a directory name.

    The single decode seam for the stores that keep the project path only in a
    name they chose. It returns a path **exactly** when the path can be
    recovered, and ``None`` otherwise — a recovered ``cwd`` drives repo-scoped
    filtering, so a fabricated one does not merely omit a result, it makes
    agentgrep silently miss the user's own project while reporting success.

    :attr:`OriginEncoding.URL` is lossless: percent-decoding is a recovery.
    :attr:`OriginEncoding.DASH` is not. Cursor CLI names a project directory by
    replacing every separator with ``-`` and escaping nothing, so ``foo-bar`` is
    equally consistent with ``/foo/bar`` and ``/foo-bar``. Measured against 100
    live project directories, the naive ``"/" + name.replace("-", "/")`` inverse
    named a directory that exists 17 times and invented an absolute path the
    other 83. Dash names are therefore reconstructed against the filesystem and
    accepted only when exactly one split resolves to a directory that exists —
    29 of the same 100 resolve, and the other 71 stay a known-unknown.

    Parameters
    ----------
    name : str
        The directory name the store wrote.
    encoding : OriginEncoding
        How ``name`` encodes the path.
    cache : ProjectDirCache, optional
        Caller-owned memo for the filesystem-probing dash decode.

    Returns
    -------
    str or None
        The decoded working directory, or ``None`` when it cannot be recovered
        exactly.

    Examples
    --------
    >>> decode_project_dir("%2Fwork%2Fproj", encoding=OriginEncoding.URL)
    '/work/proj'

    A name that does not decode to something path-shaped is refused rather than
    guessed at:

    >>> decode_project_dir("session-1234", encoding=OriginEncoding.URL) is None
    True
    """
    text = name.strip()
    if not text:
        return None
    if encoding is OriginEncoding.URL:
        return _path_like_text(urllib.parse.unquote(text))
    return _dash_decode_unique_dir(text, cache=cache)


def _path_like_text(value: str) -> str | None:
    """Accept a decoded value as a path only when it looks like one."""
    decoded = value.strip()
    if not decoded or not is_path_like_text(decoded):
        return None
    return decoded


def _dash_decode_unique_dir(name: str, *, cache: ProjectDirCache | None) -> str | None:
    """Reconstruct a dash-encoded directory, memoized through ``cache``.

    An unresolvable name is cached as ``None``: refusing to resolve costs the
    same directory probes as resolving, so the miss is worth remembering too.
    """
    if cache is None:
        return _reconstruct_dashed_dir(name)
    key = f"{OriginEncoding.DASH.value}:{name}"
    if key in cache:
        return cache[key]
    resolved = _reconstruct_dashed_dir(name)
    cache[key] = resolved
    return resolved


def _reconstruct_dashed_dir(name: str) -> str | None:
    """Return the one existing directory ``name`` reconstructs to, if unique.

    Each dash is either a path separator or a literal dash inside one directory
    name, so the walk is filesystem-directed: only prefixes that exist on disk
    are expanded. Empty tokens are preserved rather than dropped — a project
    whose name contains a literal ``--`` encodes to two adjacent dashes, and
    discarding the empty token between them makes that project unresolvable.
    """
    encoded = name.removeprefix("-")
    tokens = encoded.split("-")
    if len(tokens) > DASH_DECODE_MAX_TOKENS:
        return None
    budget = DASH_DECODE_PROBE_BUDGET
    resolutions: list[str] = []
    # Depth-first over the splits that survive on disk. Each frame is a
    # directory that exists plus the index of the first unconsumed token.
    stack: list[tuple[pathlib.Path, int]] = [(pathlib.Path("/"), 0)]
    while stack:
        current, index = stack.pop()
        if index == len(tokens):
            resolutions.append(str(current))
            if len(resolutions) > 1:
                # Two real directories encode to this name: refuse to pick one.
                return None
            continue
        for end in range(index + 1, len(tokens) + 1):
            segment = "-".join(tokens[index:end])
            if not segment:
                # A directory component is never empty; a leading or trailing
                # dash in the encoded name contributes no component of its own.
                continue
            if budget <= 0:
                return None
            budget -= 1
            candidate = current / segment
            if candidate.is_dir():
                stack.append((candidate, end))
    return resolutions[0] if len(resolutions) == 1 else None


def normalize_origin_path_text(value: str | None) -> str | None:
    """Expand and absolutize a user-supplied origin path filter value.

    CLI and MCP frontends share this so ``--cwd .`` and an MCP
    ``cwd="."`` resolve the same way before matching against the
    absolute paths records carry. The value keeps the user's logical
    (unresolved) form; symlink resolution happens as an extra pattern
    variant at compile time so records stored in either form match.
    """
    if value is None or not value.strip():
        # A blank value must not resolve to the invoking process's cwd.
        return None
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute():
        path = _logical_cwd() / path
    return os.path.normpath(str(path))


def _logical_cwd() -> pathlib.Path:
    physical = pathlib.Path.cwd()
    pwd = os.environ.get("PWD")
    if pwd:
        candidate = pathlib.Path(pwd).expanduser()
        if candidate.is_absolute() and _same_physical_path(candidate, physical):
            return pathlib.Path(os.path.normpath(str(candidate)))
    return physical


def _same_physical_path(left: pathlib.Path, right: pathlib.Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except OSError:
        return False


def origin_filter_nodes(
    *,
    cwd: str | None = None,
    repo: str | None = None,
    worktree: str | None = None,
    branch: str | None = None,
    cwd_hash: str | None = None,
) -> tuple[FieldEqNode, ...]:
    """Return synthetic field predicates for explicit origin filters.

    Values land verbatim in AST nodes for
    :func:`agentgrep.query.compose_query_ast`, so no query-text quoting
    or escaping is involved.
    """
    # Deferred: keeps pydantic-backed AST models off the CLI cold-start path.
    from agentgrep.query.ast import FieldEqNode

    return tuple(
        FieldEqNode(field=field, value=value)
        for field, value in (
            ("cwd", cwd),
            ("repo", repo),
            ("worktree", worktree),
            ("branch", branch),
            ("cwd_hash", cwd_hash),
        )
        if value and value.strip()
    )


def record_origin_field_values(record: SearchRecord, field: str) -> tuple[str, ...]:
    """Return every known value for an origin query field on ``record``."""
    origin = record.origin
    if field in _PATH_FIELD_KEYS:
        values: list[str] = []
        if origin is not None:
            value = t.cast("str | None", getattr(origin, field))
            if value:
                values.append(value)
            if field == "repo":
                values.extend(value for value in (origin.worktree, origin.cwd) if value)
        values.extend(_metadata_strings(record, _PATH_FIELD_KEYS[field]))
        return _dedupe(values)
    if field in _STRING_FIELD_KEYS:
        values = []
        if origin is not None:
            value = t.cast("str | None", getattr(origin, field))
            if value:
                values.append(value)
        values.extend(_metadata_strings(record, _STRING_FIELD_KEYS[field]))
        return _dedupe(values)
    if field == "project":
        return _project_values(record)
    return ()


def origin_field_values(origin: RecordOrigin, field: str) -> tuple[str, ...]:
    """Return values for an origin query field on one origin object."""
    if field in _PATH_FIELD_KEYS:
        values: list[str] = []
        value = t.cast("str | None", getattr(origin, field))
        if value:
            values.append(value)
        if field == "repo":
            values.extend(value for value in (origin.worktree, origin.cwd) if value)
        return _dedupe(values)
    if field in _STRING_FIELD_KEYS:
        value = t.cast("str | None", getattr(origin, field))
        return () if not value else (value,)
    if field == "project":
        values: list[str] = []
        for value in (origin.repo, origin.worktree, origin.cwd):
            _append_project_value(values, value)
        return _dedupe(values)
    return ()


def record_matches_origin(record: SearchRecord, origin: RecordOrigin | None) -> bool:
    """Return whether ``record`` belongs to the supplied origin context.

    Ranking uses this for opt-in same-project boosts. Path comparisons accept
    descendants, so a target repo of ``/repo`` matches a record cwd of
    ``/repo/src``.
    """
    return OriginMatcher.from_origin(origin).matches(record)


def _metadata_strings(record: SearchRecord, keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for key in keys:
        value = record.metadata.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return values


def _project_values(record: SearchRecord) -> tuple[str, ...]:
    values: list[str] = []
    if record.origin is not None:
        for value in (record.origin.repo, record.origin.worktree, record.origin.cwd):
            _append_project_value(values, value)
    for key in ("project", "workspace", "directory", "cwd", "repo", "worktree"):
        value = record.metadata.get(key)
        if isinstance(value, str) and value.strip():
            _append_project_value(values, value.strip())
    return _dedupe(values)


def _append_project_value(values: list[str], value: str | None) -> None:
    if not value:
        return
    values.append(value)
    normalized = value.rstrip("/\\")
    if not normalized:
        return
    if "/" in normalized or "\\" in normalized:
        name = pathlib.PurePosixPath(normalized.replace("\\", "/")).name
        if name:
            values.append(name)


def _origin_string_is_glob(value: str) -> bool:
    return "*" in value or "?" in value


def _origin_path_is_glob(values: t.Iterable[str]) -> bool:
    return any(ch in value for value in values for ch in "*?[")


def _origin_string_equal(value: str, target: str, *, is_glob: bool) -> bool:
    value_cmp = value.casefold()
    target_cmp = target.casefold()
    if is_glob:
        return fnmatch.fnmatchcase(value_cmp, target_cmp)
    return value_cmp == target_cmp


def _origin_path_boundary_text(value: str) -> str:
    """Normalize a path for boundary comparison (shared by filter and boost)."""
    if value == "~" or value.startswith("~/"):
        value = str(pathlib.Path(value).expanduser())
    normalized = value.replace("\\", "/")
    stripped = normalized.rstrip("/")
    if stripped:
        return stripped
    if normalized.startswith("/"):
        return "/"
    return normalized


def _path_is_equal_or_descendant(path: str, target: str) -> bool:
    if not path or not target:
        return False
    if target == "/":
        return path.startswith("/")
    return path == target or path.startswith(f"{target}/")


def _dedupe(values: t.Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)
