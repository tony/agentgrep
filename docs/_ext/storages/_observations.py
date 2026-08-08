"""Read ``observations/<agent>/<version>.toml`` into one index per build.

Only schema facts are kept. Source counts and the unclaimed list describe the
machine an observation ran on, so they are dropped here rather than filtered
at render time.
"""

from __future__ import annotations

import datetime
import pathlib
import tomllib
import typing as t

if t.TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

MANIFEST_VERSION = 1
"""Manifest schema this reader understands; see ``scripts/observe_stores.py``."""

WILDCARD_BUCKET = "*"
"""Bucket a manifest uses for a store with no discriminator."""

UNKNOWN_VERSION = "unknown"
"""Recorded when the observer could not read an app version."""

_KeyMap = tuple[tuple[str, tuple[str, ...]], ...]


class ObservedShape(t.NamedTuple):
    """Schema facts recorded for one catalogue store."""

    store_id: str
    discriminator: str
    record_keys: _KeyMap
    tables: _KeyMap

    @property
    def is_empty(self) -> bool:
        """Whether the manifest recorded nothing renderable.

        >>> ObservedShape("grok.logs", "", (), ()).is_empty
        True
        """
        return not (self.discriminator or self.record_keys or self.tables)


class AgentObservation(t.NamedTuple):
    """The newest manifest for one agent, keyed by catalogue ``store_id``."""

    agent: str
    app_version: str
    observed_at: str
    shapes: Mapping[str, ObservedShape]


class ObservationIndex(t.NamedTuple):
    """Every agent's newest manifest, plus unreadable-file messages.

    ``paths`` lists every manifest on disk, not just the selected ones. Pages
    depend on all of them so Sphinx notices an edit or a deletion on its own.
    """

    agents: Mapping[str, AgentObservation]
    problems: tuple[str, ...]
    paths: tuple[pathlib.Path, ...] = ()

    def observed(self, agent: str, store_id: str) -> tuple[AgentObservation, ObservedShape] | None:
        """Return the manifest and shape for one store, or ``None``."""
        observation = self.agents.get(agent)
        if observation is None:
            return None
        shape = observation.shapes.get(store_id)
        return None if shape is None else (observation, shape)


class VersionDrift(t.NamedTuple):
    """An agent whose catalogue stamp and newest manifest disagree."""

    agent: str
    catalog_versions: tuple[str, ...]
    manifest_version: str

    @property
    def message(self) -> str:
        """Warning naming both repairs, since either side may be the stale one.

        >>> message = VersionDrift("cursor-cli", ("cursor-agent 1.0",), "2.0").message
        >>> "observe --agent cursor-cli" in message
        True
        >>> "store_catalog/cursor_cli.py" in message
        True
        """
        stamps = ", ".join(repr(stamp) for stamp in self.catalog_versions)
        module = self.agent.replace("-", "_")
        return (
            f"storage observations: {self.agent} catalogue says {stamps}, "
            f"newest manifest says {self.manifest_version!r}; re-observe with "
            f"`uv run scripts/observe_stores.py observe --agent {self.agent}`, "
            f"or bump the observed_version constant in "
            f"src/agentgrep/store_catalog/{module}.py"
        )


def version_token(observed_version: str) -> str:
    """Return the bare version from a catalogue stamp.

    Stamps are prose (``claude-code v2.1.226``); manifests hold the bare
    version. A leading ``v`` before a digit is decoration.

    >>> version_token("claude-code v2.1.226")
    '2.1.226'
    >>> version_token("cursor-agent 2026.08.04-aaa8809")
    '2026.08.04-aaa8809'
    >>> version_token("   ")
    ''
    """
    parts = observed_version.rsplit(maxsplit=1)
    if not parts:
        return ""
    token = parts[-1]
    if len(token) > 1 and token[0] == "v" and token[1].isdigit():
        return token[1:]
    return token


def detect_version_drift(
    index: ObservationIndex,
    catalog_versions: Mapping[str, Sequence[str]],
) -> tuple[VersionDrift, ...]:
    """Return drift per agent whose stamp and manifest disagree.

    Agents with no manifest, or whose manifest could not read a version, are
    skipped — neither contradicts the catalogue.

    >>> index = ObservationIndex(
    ...     {"grok": AgentObservation("grok", "1.1.0", "2026-08-08", {})}, ()
    ... )
    >>> [d.agent for d in detect_version_drift(index, {"grok": ("grok 1.0.0",)})]
    ['grok']
    >>> detect_version_drift(index, {"grok": ("grok v1.1.0",)})
    ()
    """
    drifts: list[VersionDrift] = []
    for agent in sorted(catalog_versions):
        observation = index.agents.get(agent)
        if observation is None or observation.app_version == UNKNOWN_VERSION:
            continue
        observed = version_token(observation.app_version)
        stamps = tuple(catalog_versions[agent])
        if all(version_token(stamp) != observed for stamp in stamps):
            drifts.append(VersionDrift(agent, stamps, observation.app_version))
    return tuple(drifts)


def load_observation_index(root: pathlib.Path) -> ObservationIndex:
    """Read every agent's newest manifest under *root*. A missing tree is silence."""
    if not root.is_dir():
        return ObservationIndex({}, ())

    agents: dict[str, AgentObservation] = {}
    problems: list[str] = []
    for agent_dir in sorted(entry for entry in root.iterdir() if entry.is_dir()):
        observation, agent_problems = _load_agent(agent_dir)
        problems.extend(agent_problems)
        if observation is not None:
            agents[observation.agent] = observation
    return ObservationIndex(agents, tuple(problems), tuple(sorted(root.glob("*/*.toml"))))


def _load_agent(agent_dir: pathlib.Path) -> tuple[AgentObservation | None, tuple[str, ...]]:
    """Return the newest manifest in one agent directory, plus read problems."""
    newest: tuple[datetime.date, str, AgentObservation] | None = None
    problems: list[str] = []
    for candidate in sorted(agent_dir.glob("*.toml")):
        label = f"{agent_dir.name}/{candidate.name}"
        try:
            raw = candidate.read_bytes()
            payload = tomllib.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            problems.append(f"{label}: {exc}")
            continue
        found = payload.get("manifest_version")
        if found != MANIFEST_VERSION:
            problems.append(f"{label}: manifest_version {found!r}, expected {MANIFEST_VERSION}")
            continue
        observed_at = _observation_date(payload)
        if observed_at is None:
            problems.append(f"{label}: missing observation.observed_at")
            continue
        key = (observed_at, candidate.name)
        if newest is not None and key <= newest[:2]:
            continue
        newest = (observed_at, candidate.name, _observation(agent_dir.name, payload, observed_at))
    return (None if newest is None else newest[2]), tuple(problems)


def _observation(
    agent: str,
    payload: Mapping[str, object],
    observed_at: datetime.date,
) -> AgentObservation:
    """Build one observation from a parsed manifest."""
    agent_table = payload.get("agent")
    app_version = UNKNOWN_VERSION
    if isinstance(agent_table, dict):
        app_version = str(agent_table.get("app_version") or UNKNOWN_VERSION)

    shapes: dict[str, ObservedShape] = {}
    stores = payload.get("store")
    if isinstance(stores, list):
        for entry in t.cast("list[object]", stores):
            shape = _shape(entry)
            if shape is not None:
                shapes[shape.store_id] = shape

    return AgentObservation(
        agent=agent,
        app_version=app_version,
        observed_at=observed_at.isoformat(),
        shapes=shapes,
    )


def _shape(entry: object) -> ObservedShape | None:
    """Return one ``[[store]]`` entry's schema facts, or ``None`` if it has none."""
    if not isinstance(entry, dict):
        return None
    table = t.cast("dict[str, object]", entry)
    store_id = str(table.get("id") or "")
    if not store_id:
        return None
    shape = ObservedShape(
        store_id=store_id,
        discriminator=str(table.get("discriminator") or ""),
        record_keys=_key_map(table.get("record_keys")),
        tables=_key_map(table.get("tables")),
    )
    return None if shape.is_empty else shape


def _key_map(value: object) -> _KeyMap:
    """Return ``(name, values)`` pairs, preserving manifest order.

    >>> _key_map({"user": ["type", "content"]})
    (('user', ('type', 'content')),)
    >>> _key_map(None)
    ()
    """
    if not isinstance(value, dict):
        return ()
    table = t.cast("dict[str, object]", value)
    return tuple((str(name), _strings(items)) for name, items in table.items())


def _strings(value: object) -> tuple[str, ...]:
    """Return a tuple of strings from an untyped TOML array."""
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in t.cast("list[object]", value))


def _observation_date(payload: Mapping[str, object]) -> datetime.date | None:
    """Return ``observation.observed_at``, if present."""
    observation = payload.get("observation")
    if not isinstance(observation, dict):
        return None
    value = t.cast("dict[str, object]", observation).get("observed_at")
    if isinstance(value, datetime.datetime):
        return value.date()
    return value if isinstance(value, datetime.date) else None
