"""Review-only suggestion artifacts built from insight findings."""

from __future__ import annotations

import dataclasses
import pathlib
import sqlite3

from agentgrep.db import DbStore, text_hash
from agentgrep.insights import InsightEngine


@dataclasses.dataclass(frozen=True, slots=True)
class SuggestionArtifact:
    """A reviewable instruction or skill suggestion."""

    suggestion_id: str
    run_id: str
    target_path: pathlib.Path
    surface_kind: str
    title: str
    body: str
    confidence: float
    status: str
    rationale: str
    reload_note: str


class SuggestionEngine:
    """Create and read review-only suggestion artifacts."""

    def __init__(self, store: DbStore) -> None:
        self.store = store

    def create_from_omissions(self, *, target_path: pathlib.Path) -> list[SuggestionArtifact]:
        """Create suggestions from open omission findings for ``target_path``."""
        insights = InsightEngine(self.store)
        findings = insights.list_omission_findings(target_path=target_path)
        suggestions: list[SuggestionArtifact] = []
        with self.store.connection:
            for finding in findings:
                row = self.store.get_record_row(finding.representative_record_id)
                if row is None:
                    continue
                suggestion_id = text_hash(
                    f"suggestion\0{finding.finding_id}\0{target_path}",
                )[:32]
                title = "Add missing agent instruction"
                body = row.record.text.strip()
                reload_note = (
                    "This suggestion takes effect only after the patch is accepted "
                    "and the relevant agent session reloads or restarts."
                )
                artifact = SuggestionArtifact(
                    suggestion_id=suggestion_id,
                    run_id=finding.run_id,
                    target_path=target_path,
                    surface_kind="agents_md",
                    title=title,
                    body=body,
                    confidence=finding.confidence,
                    status="proposed",
                    rationale=finding.rationale,
                    reload_note=reload_note,
                )
                self.store.connection.execute(
                    """
                    INSERT OR REPLACE INTO suggestions(
                        suggestion_id, run_id, target_path, surface_kind,
                        title, body, confidence, status, rationale,
                        reload_note, created_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'deterministic')
                    """,
                    (
                        artifact.suggestion_id,
                        artifact.run_id,
                        str(artifact.target_path),
                        artifact.surface_kind,
                        artifact.title,
                        artifact.body,
                        artifact.confidence,
                        artifact.status,
                        artifact.rationale,
                        artifact.reload_note,
                    ),
                )
                self.store.connection.execute(
                    """
                    INSERT OR REPLACE INTO suggestion_evidence(
                        suggestion_id, record_id, evidence_role, score, signals_json
                    )
                    VALUES(?, ?, 'representative', ?, ?)
                    """,
                    (
                        artifact.suggestion_id,
                        row.record_id,
                        artifact.confidence,
                        '{"signal":"omission_representative"}',
                    ),
                )
                suggestions.append(artifact)
        return suggestions

    def list_suggestions(self) -> list[SuggestionArtifact]:
        """Return persisted suggestions."""
        rows = self.store.connection.execute(
            """
            SELECT suggestion_id, run_id, target_path, surface_kind, title, body,
                   confidence, status, rationale, reload_note
            FROM suggestions
            ORDER BY confidence DESC, suggestion_id
            """,
        ).fetchall()
        return [self._row_to_artifact(row) for row in rows]

    def get_suggestion(self, suggestion_id: str) -> SuggestionArtifact | None:
        """Return one suggestion by id."""
        row = self.store.connection.execute(
            """
            SELECT suggestion_id, run_id, target_path, surface_kind, title, body,
                   confidence, status, rationale, reload_note
            FROM suggestions
            WHERE suggestion_id = ?
            """,
            (suggestion_id,),
        ).fetchone()
        return None if row is None else self._row_to_artifact(row)

    def render_suggestion(self, suggestion_id: str) -> str | None:
        """Render one suggestion as reviewable text."""
        artifact = self.get_suggestion(suggestion_id)
        if artifact is None:
            return None
        return (
            f"{artifact.title}\n\n"
            f"Target: {artifact.target_path}\n"
            f"Confidence: {artifact.confidence:.2f}\n\n"
            f"{artifact.body}\n\n"
            f"{artifact.reload_note}"
        )

    def _row_to_artifact(self, row: sqlite3.Row) -> SuggestionArtifact:
        """Convert one SQLite row to a suggestion artifact."""
        return SuggestionArtifact(
            suggestion_id=str(row["suggestion_id"]),
            run_id=str(row["run_id"]),
            target_path=pathlib.Path(str(row["target_path"])),
            surface_kind=str(row["surface_kind"]),
            title=str(row["title"]),
            body=str(row["body"]),
            confidence=float(row["confidence"]),
            status=str(row["status"]),
            rationale=str(row["rationale"]),
            reload_note=str(row["reload_note"]),
        )
