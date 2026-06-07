"""Deterministic insights engine for indexed agent data."""

from __future__ import annotations

import collections
import dataclasses
import itertools
import json
import pathlib
import typing as t

import agentgrep
from agentgrep.db import (
    DbStore,
    normalize_record_text,
    text_hash,
    token_set,
)

_INSIGHT_PROGRESS_INTERVAL = 1024


@dataclasses.dataclass(frozen=True, slots=True)
class InsightRunResult:
    """Counters returned by an insight run."""

    run_id: str
    kind: str
    clusters: int = 0
    variant_edges: int = 0
    omission_findings: int = 0
    features_refreshed: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class VariantEdge:
    """One deterministic similarity or variant relationship."""

    edge_id: str
    run_id: str
    left_record_id: str
    right_record_id: str
    variant_type: str
    confidence: float
    explanation: str


@dataclasses.dataclass(frozen=True, slots=True)
class OmissionFinding:
    """One meaningful omission candidate for a target instruction surface."""

    finding_id: str
    run_id: str
    target_path: pathlib.Path
    representative_record_id: str
    confidence: float
    rationale: str


class InsightAnalyzeProgress(t.Protocol):
    """Progress sink for deterministic insight-analysis phases."""

    def set_activity(self, activity: str, *, detail: str | None = None) -> None:
        """Report the current insight-analysis activity."""


def _run_id(kind: str, payload: str) -> str:
    """Return a deterministic run id for repeatable local insight runs."""
    return text_hash(f"{kind}\0{payload}")[:24]


def _edge_id(left: str, right: str, variant_type: str) -> str:
    """Return a stable edge id independent of pair order."""
    ordered = "\0".join(sorted((left, right)))
    return text_hash(f"{variant_type}\0{ordered}")[:32]


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Return Jaccard similarity for two token sets."""
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _should_report_insight_progress(done: int, total: int) -> bool:
    """Return whether an insight loop should emit a progress update."""
    if done == total:
        return True
    interval = min(_INSIGHT_PROGRESS_INTERVAL, max(1, total // 20))
    return done % interval == 0


def _format_similarity_write_progress(
    *,
    clusters_done: int,
    clusters_total: int,
    edges_done: int,
    edges_total: int,
) -> str:
    """Return compact similarity artifact write progress."""
    return f"{clusters_done:,}/{clusters_total:,} clusters, {edges_done:,}/{edges_total:,} edges"


class InsightEngine:
    """Generate deterministic clusters, variants, and omission findings."""

    def __init__(self, store: DbStore) -> None:
        self.store = store

    def run_similarity(
        self,
        *,
        control: agentgrep.SearchControl | None = None,
        progress: InsightAnalyzeProgress | None = None,
    ) -> InsightRunResult:
        """Detect exact and near-duplicate prompt variants."""
        if control is not None and control.answer_now_requested():
            return InsightRunResult(run_id=_run_id("similarity", "cancelled"), kind="similarity")
        features_refreshed = self.store.refresh_missing_features(progress=progress)
        if control is not None and control.answer_now_requested():
            return InsightRunResult(
                run_id=_run_id("similarity", "cancelled"),
                kind="similarity",
                features_refreshed=features_refreshed,
            )
        if progress is not None:
            progress.set_activity(
                "loading similarity rows",
                detail="reading normalized record hashes",
            )
        rows = self.store.iter_similarity_rows()
        run_id = _run_id("similarity", ",".join(row.record_id for row in rows))
        now = "deterministic"
        if progress is not None:
            progress.set_activity(
                "grouping duplicate prompts",
                detail=f"{len(rows):,} normalized records",
            )
        groups: dict[str, list[str]] = collections.defaultdict(list)
        for row in rows:
            groups[row.normalized_hash].append(row.record_id)
        candidate_groups = sum(1 for record_ids in groups.values() if len(record_ids) >= 2)
        total_variant_edges = sum(
            len(record_ids) * (len(record_ids) - 1) // 2
            for record_ids in groups.values()
            if len(record_ids) >= 2
        )
        if progress is not None:
            progress.set_activity(
                "writing similarity artifacts",
                detail=_format_similarity_write_progress(
                    clusters_done=0,
                    clusters_total=candidate_groups,
                    edges_done=0,
                    edges_total=total_variant_edges,
                ),
            )
        edge_count = 0
        cluster_count = 0
        with self.store.connection:
            self._record_run(run_id, "similarity", now, {"records": len(rows)})
            for normalized_hash, record_ids in groups.items():
                if len(record_ids) < 2:
                    continue
                cluster_count += 1
                cluster_id = text_hash(f"cluster\0{normalized_hash}")[:32]
                self.store.connection.execute(
                    """
                    INSERT OR REPLACE INTO clusters(
                        cluster_id, run_id, kind, label, centroid_record_id,
                        confidence, evidence_json
                    )
                    VALUES(?, ?, 'duplicate_prompt', ?, ?, 1.0, ?)
                    """,
                    (
                        cluster_id,
                        run_id,
                        "exact duplicate prompt family",
                        record_ids[0],
                        '{"signal":"normalized_hash"}',
                    ),
                )
                for record_id in record_ids:
                    self.store.connection.execute(
                        """
                        INSERT OR REPLACE INTO cluster_members(
                            cluster_id, record_id, score, signals_json
                        )
                        VALUES(?, ?, 1.0, ?)
                        """,
                        (cluster_id, record_id, '{"normalized_hash":1.0}'),
                    )
                for left, right in itertools.combinations(record_ids, 2):
                    edge_id = _edge_id(left, right, "exact_duplicate")
                    self.store.connection.execute(
                        """
                        INSERT OR REPLACE INTO variant_edges(
                            edge_id, run_id, left_record_id, right_record_id,
                            variant_type, confidence, signals_json, explanation
                        )
                        VALUES(?, ?, ?, ?, 'exact_duplicate', 1.0, ?, ?)
                        """,
                        (
                            edge_id,
                            run_id,
                            left,
                            right,
                            '{"normalized_hash":1.0}',
                            "normalized prompt text is identical",
                        ),
                    )
                    edge_count += 1
                    if progress is not None and _should_report_insight_progress(
                        edge_count,
                        total_variant_edges,
                    ):
                        progress.set_activity(
                            "writing similarity artifacts",
                            detail=_format_similarity_write_progress(
                                clusters_done=cluster_count,
                                clusters_total=candidate_groups,
                                edges_done=edge_count,
                                edges_total=total_variant_edges,
                            ),
                        )
        if progress is not None:
            progress.set_activity(
                "writing similarity artifacts",
                detail=_format_similarity_write_progress(
                    clusters_done=cluster_count,
                    clusters_total=candidate_groups,
                    edges_done=edge_count,
                    edges_total=total_variant_edges,
                ),
            )
        return InsightRunResult(
            run_id=run_id,
            kind="similarity",
            clusters=cluster_count,
            variant_edges=edge_count,
            features_refreshed=features_refreshed,
        )

    def run_omissions(
        self,
        *,
        target_path: pathlib.Path,
        target_text: str,
        control: agentgrep.SearchControl | None = None,
        progress: InsightAnalyzeProgress | None = None,
    ) -> InsightRunResult:
        """Detect indexed instructions absent from a target file."""
        if control is not None and control.answer_now_requested():
            return InsightRunResult(run_id=_run_id("omissions", "cancelled"), kind="omissions")
        features_refreshed = self.store.refresh_missing_features(progress=progress)
        if control is not None and control.answer_now_requested():
            return InsightRunResult(
                run_id=_run_id("omissions", "cancelled"),
                kind="omissions",
                features_refreshed=features_refreshed,
            )
        if progress is not None:
            progress.set_activity(
                "loading indexed records",
                detail="reading prompt and instruction text",
            )
        rows = self.store.iter_record_rows()
        run_id = _run_id("omissions", f"{target_path}\0{target_text}\0{len(rows)}")
        if progress is not None:
            progress.set_activity(
                "normalizing target text",
                detail=str(target_path),
            )
        target_normalized = normalize_record_text(target_text)
        target_tokens = token_set(target_text)
        finding_count = 0
        if progress is not None:
            progress.set_activity(
                "comparing omission candidates",
                detail=f"{len(rows):,} indexed records",
            )
        with self.store.connection:
            self._record_run(
                run_id,
                "omissions",
                "deterministic",
                {"target_path": str(target_path), "records": len(rows)},
            )
            for row in rows:
                record_normalized = normalize_record_text(row.record.text)
                if not record_normalized or record_normalized in target_normalized:
                    continue
                record_tokens = token_set(row.record.text)
                if len(record_tokens) < 3:
                    continue
                overlap = _jaccard(record_tokens, target_tokens)
                confidence = max(0.72, min(0.95, 0.72 + overlap / 4))
                finding_id = text_hash(
                    f"omission\0{target_path}\0{row.record_id}\0{run_id}",
                )[:32]
                self.store.connection.execute(
                    """
                    INSERT OR REPLACE INTO omission_findings(
                        finding_id, run_id, target_path, cluster_id,
                        representative_record_id, confidence, status,
                        evidence_json, rationale
                    )
                    VALUES(?, ?, ?, NULL, ?, ?, 'open', ?, ?)
                    """,
                    (
                        finding_id,
                        run_id,
                        str(target_path),
                        row.record_id,
                        confidence,
                        '{"signal":"absent_instruction"}',
                        "indexed instruction is absent from the target surface",
                    ),
                )
                finding_count += 1
        if progress is not None:
            progress.set_activity(
                "writing omission findings",
                detail=f"{finding_count:,} findings",
            )
        return InsightRunResult(
            run_id=run_id,
            kind="omissions",
            omission_findings=finding_count,
            features_refreshed=features_refreshed,
        )

    def count_variant_edges(self) -> int:
        """Return the number of persisted variant edges."""
        row = self.store.connection.execute(
            "SELECT COUNT(*) AS count FROM variant_edges",
        ).fetchone()
        return int(row["count"])

    def list_variant_edges(self, *, limit: int | None = None) -> list[VariantEdge]:
        """Return persisted variant edges."""
        sql = """
            SELECT edge_id, run_id, left_record_id, right_record_id,
                   variant_type, confidence, explanation
            FROM variant_edges
            ORDER BY confidence DESC, edge_id
        """
        params: tuple[object, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        rows = self.store.connection.execute(sql, params).fetchall()
        return [
            VariantEdge(
                edge_id=str(row["edge_id"]),
                run_id=str(row["run_id"]),
                left_record_id=str(row["left_record_id"]),
                right_record_id=str(row["right_record_id"]),
                variant_type=str(row["variant_type"]),
                confidence=float(row["confidence"]),
                explanation=str(row["explanation"]),
            )
            for row in rows
        ]

    def list_omission_findings(
        self,
        *,
        target_path: pathlib.Path | None = None,
        limit: int | None = None,
    ) -> list[OmissionFinding]:
        """Return persisted omission findings."""
        params: list[object] = []
        if target_path is None:
            sql = """
                SELECT finding_id, run_id, target_path, representative_record_id,
                       confidence, rationale
                FROM omission_findings
                ORDER BY confidence DESC, finding_id
                """
        else:
            sql = """
                SELECT finding_id, run_id, target_path, representative_record_id,
                       confidence, rationale
                FROM omission_findings
                WHERE target_path = ?
                ORDER BY confidence DESC, finding_id
                """
            params.append(str(target_path))
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.store.connection.execute(sql, tuple(params)).fetchall()
        return [
            OmissionFinding(
                finding_id=str(row["finding_id"]),
                run_id=str(row["run_id"]),
                target_path=pathlib.Path(str(row["target_path"])),
                representative_record_id=str(row["representative_record_id"]),
                confidence=float(row["confidence"]),
                rationale=str(row["rationale"]),
            )
            for row in rows
        ]

    def count_omission_findings(
        self,
        *,
        target_path: pathlib.Path | None = None,
    ) -> int:
        """Return the number of persisted omission findings."""
        if target_path is None:
            row = self.store.connection.execute(
                "SELECT COUNT(*) AS count FROM omission_findings",
            ).fetchone()
        else:
            row = self.store.connection.execute(
                "SELECT COUNT(*) AS count FROM omission_findings WHERE target_path = ?",
                (str(target_path),),
            ).fetchone()
        return int(row["count"])

    def _record_run(self, run_id: str, kind: str, now: str, counters: dict[str, object]) -> None:
        """Upsert one insight run row."""
        self.store.connection.execute(
            """
            INSERT OR REPLACE INTO insight_runs(
                run_id, kind, started_at, finished_at, status,
                algorithm_version, input_json, counters_json
            )
            VALUES(?, ?, ?, ?, 'ok', 'deterministic-v1', '{}', ?)
            """,
            (run_id, kind, now, now, json.dumps(counters, sort_keys=True)),
        )
