"""Typed dispatch vocabulary for the adapter parser registry.

Each per-agent module declares its adapter ids as a tuple of
:class:`ParserSpec` / :class:`StreamParserSpec` rows; the package facade
merges the fragments into the one dispatch mapping ``iter_source_records``
reads. The two spec shapes encode the planning-visible stream contract in
the type: a :class:`StreamParserSpec` parser receives the engine's
``raw_skip_line``/``reverse`` arguments, a :class:`ParserSpec` parser never
sees them.
"""

from __future__ import annotations

import collections.abc as cabc
import typing as t

from agentgrep.records import RawJsonlSkipLine, SearchRecord, SourceHandle

SourceParser = cabc.Callable[[SourceHandle], cabc.Iterator[SearchRecord]]
"""A parser dispatched with the source handle alone."""


class StreamParser(t.Protocol):
    """A stream-aware JSONL parser (planning-visible contract, ADR 0004).

    The engine's scan strategies forward a raw line-skip predicate and a
    bounded-reverse flag to exactly these parsers; which adapter ids carry
    the contract is part of query planning, so the shape lives in the spec
    type rather than in per-branch call sites.
    """

    def __call__(
        self,
        source: SourceHandle,
        *,
        raw_skip_line: RawJsonlSkipLine | None = None,
        reverse: bool = False,
    ) -> cabc.Iterator[SearchRecord]:
        """Yield records, honouring the raw-prefilter/reverse contract."""
        ...


class ParserSpec(t.NamedTuple):
    """One ``adapter_id`` -> plain parser dispatch row."""

    adapter_id: str
    parser: SourceParser


class StreamParserSpec(t.NamedTuple):
    """One ``adapter_id`` -> stream-aware parser dispatch row."""

    adapter_id: str
    parser: StreamParser


AnyParserSpec = ParserSpec | StreamParserSpec
"""Either dispatch-row shape; ``iter_source_records`` narrows on the type."""


def merge_parser_specs(
    *fragments: tuple[AnyParserSpec, ...],
) -> dict[str, AnyParserSpec]:
    """Merge per-agent registry fragments into one dispatch mapping.

    Rejects duplicate adapter ids so two fragments can never silently
    shadow each other.

    Parameters
    ----------
    *fragments : tuple[AnyParserSpec, ...]
        Per-agent spec tuples, one per module.

    Returns
    -------
    dict[str, AnyParserSpec]
        ``adapter_id`` -> spec, insertion-ordered by fragment.

    Examples
    --------
    >>> def _parse(source):
    ...     yield from ()
    >>> merged = merge_parser_specs((ParserSpec("demo.jsonl.v1", _parse),))
    >>> sorted(merged)
    ['demo.jsonl.v1']
    >>> merge_parser_specs(
    ...     (ParserSpec("demo.jsonl.v1", _parse),),
    ...     (ParserSpec("demo.jsonl.v1", _parse),),
    ... )
    Traceback (most recent call last):
        ...
    ValueError: duplicate adapter id in parser registry: 'demo.jsonl.v1'
    """
    registry: dict[str, AnyParserSpec] = {}
    for fragment in fragments:
        for spec in fragment:
            if spec.adapter_id in registry:
                msg = f"duplicate adapter id in parser registry: {spec.adapter_id!r}"
                raise ValueError(msg)
            registry[spec.adapter_id] = spec
    return registry
