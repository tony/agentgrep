"""Persistent, inspectable store for the insights graph engine.

One SQLite database (under the index cache) holds both the vectors and the
graph: a ``sqlite-vec`` ``vec0`` table per node type for cosine KNN, plus
plain relational tables for nodes, typed edges, and mined workflow
sequences. Everything is in one removable file, matching the ADR-0005
"inspectable, embedded, easy to remove" index principle.

The ``sqlite_vec`` module is injected (loaded lazily by the graph
enricher), so importing this module pulls in nothing heavier than the
standard library.
"""

from __future__ import annotations

import json
import sqlite3
import typing as t

if t.TYPE_CHECKING:
    import pathlib

SCHEMA_VERSION = 1

_VEC_TABLES: dict[str, str] = {
    "prompt": "vec_prompts",
    "reply": "vec_replies",
    "exchange": "vec_exchanges",
    "conversation": "vec_conversations",
}


class GraphStore:
    """A sqlite-vec + sqlite graph database for the insights network."""

    def __init__(self, connection: sqlite3.Connection, sqlite_vec: t.Any, dim: int) -> None:
        self._conn = connection
        self._serialize = sqlite_vec.serialize_float32
        self._dim = dim

    @classmethod
    def open(
        cls,
        path: pathlib.Path,
        *,
        sqlite_vec: t.Any,
        dim: int,
        model_id: str | None = None,
    ) -> GraphStore:
        """Open (creating if needed) the graph database at ``path``.

        ``model_id`` and ``dim`` are recorded in ``meta``. If a persisted
        store was built with a different schema version, embedding model, or
        vector dimension, its tables are dropped and rebuilt — stale vectors
        from another model are incompatible and must not be mixed with fresh
        ones.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path))
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        connection.enable_load_extension(False)
        store = cls(connection, sqlite_vec, dim)
        store._ensure_schema(model_id=model_id)
        return store

    def _stored_meta(self, key: str) -> str | None:
        """Return a ``meta`` value, or ``None`` when the table/row is absent."""
        if not self._table_exists("meta"):
            return None
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def _is_stale(self, *, model_id: str | None) -> bool:
        """Return whether a persisted store is incompatible with this run."""
        stored_version = self._stored_meta("schema_version")
        if stored_version is not None and stored_version != str(SCHEMA_VERSION):
            return True
        stored_dim = self._stored_meta("dim")
        if stored_dim is not None and stored_dim != str(self._dim):
            return True
        stored_model = self._stored_meta("embedding_model")
        return stored_model is not None and model_id is not None and stored_model != model_id

    def _drop_all(self) -> None:
        """Drop every vector and relational table (schema/model rebuild)."""
        for table in _VEC_TABLES.values():
            self._conn.execute(f"DROP TABLE IF EXISTS {table}")
        for table in ("nodes", "edges", "sequences", "summaries", "meta"):
            self._conn.execute(f"DROP TABLE IF EXISTS {table}")
        self._conn.commit()

    def get_summary(self, content_hash: str) -> str | None:
        """Return a cached conversation summary by content hash, if present."""
        row = self._conn.execute(
            "SELECT summary FROM summaries WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return None if row is None else str(row[0])

    def set_summary(self, content_hash: str, summary: str) -> None:
        """Cache a conversation summary keyed by its content hash."""
        self._conn.execute(
            "INSERT OR REPLACE INTO summaries(content_hash, summary) VALUES (?, ?)",
            (content_hash, summary),
        )

    def _ensure_schema(self, *, model_id: str | None = None) -> None:
        """Create vector and graph tables, rebuilding on schema/model change."""
        if self._is_stale(model_id=model_id):
            self._drop_all()
        for table in _VEC_TABLES.values():
            if not self._table_exists(table):
                self._conn.execute(
                    f"CREATE VIRTUAL TABLE {table} USING vec0("
                    f"node_id text, conversation_id text, "
                    f"embedding float[{self._dim}] distance_metric=cosine, +text text)"
                )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS nodes ("
            "node_id TEXT PRIMARY KEY, node_type TEXT, conversation_id TEXT, "
            "text TEXT, content_hash TEXT, archetype INTEGER)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS edges (src TEXT, dst TEXT, kind TEXT, weight REAL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS sequences ("
            "rank INTEGER, pattern TEXT, support INTEGER, example TEXT)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS summaries (content_hash TEXT PRIMARY KEY, summary TEXT)"
        )
        self._conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        meta_rows = [("schema_version", str(SCHEMA_VERSION)), ("dim", str(self._dim))]
        if model_id is not None:
            meta_rows.append(("embedding_model", model_id))
        self._conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", meta_rows)
        self._conn.commit()

    def _table_exists(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','virtual') AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def existing_hashes(self) -> set[str]:
        """Return the content hashes already persisted (for incremental skip)."""
        return {row[0] for row in self._conn.execute("SELECT content_hash FROM nodes")}

    def vectors_by_hash(self, node_type: str, numpy: t.Any) -> dict[str, t.Any]:
        """Return ``content_hash -> vector`` for persisted nodes of a type.

        Lets a rerun reuse embeddings for unchanged turns instead of
        re-encoding them. Vectors come back as float32 numpy arrays
        deserialized from the ``vec0`` ``embedding`` column.
        """
        table = _VEC_TABLES[node_type]
        rows = self._conn.execute(
            f"SELECT n.content_hash, v.embedding FROM nodes n "
            f"JOIN {table} v ON n.node_id = v.node_id WHERE n.node_type = ?",
            (node_type,),
        ).fetchall()
        out: dict[str, t.Any] = {}
        for content_hash, blob in rows:
            if content_hash is None or blob is None:
                continue
            out[str(content_hash)] = numpy.frombuffer(blob, dtype="<f4")
        return out

    def add_node(
        self,
        *,
        node_id: str,
        node_type: str,
        conversation_id: str,
        text: str,
        content_hash: str,
        vector: list[float],
        archetype: int | None = None,
    ) -> bool:
        """Insert a node + its vector, idempotently. Returns whether it was new."""
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO nodes("
            "node_id, node_type, conversation_id, text, content_hash, archetype) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (node_id, node_type, conversation_id, text, content_hash, archetype),
        )
        if cursor.rowcount == 0:
            return False
        table = _VEC_TABLES[node_type]
        self._conn.execute(
            f"INSERT INTO {table}(node_id, conversation_id, embedding, text) VALUES (?, ?, ?, ?)",
            (node_id, conversation_id, self._serialize(vector), text[:2000]),
        )
        return True

    def set_archetype(self, node_id: str, archetype: int) -> None:
        """Record the archetype cluster id for a node."""
        self._conn.execute("UPDATE nodes SET archetype = ? WHERE node_id = ?", (archetype, node_id))

    def replace_edges(self, edges: t.Iterable[tuple[str, str, str, float]]) -> None:
        """Replace all edges (derived each run; cheap to recompute)."""
        self._conn.execute("DELETE FROM edges")
        self._conn.executemany(
            "INSERT INTO edges(src, dst, kind, weight) VALUES (?, ?, ?, ?)", edges
        )

    def replace_sequences(self, rows: t.Iterable[tuple[int, list[int], int, str]]) -> None:
        """Replace mined workflow sequences."""
        self._conn.execute("DELETE FROM sequences")
        self._conn.executemany(
            "INSERT INTO sequences(rank, pattern, support, example) VALUES (?, ?, ?, ?)",
            (
                (rank, json.dumps(pattern), support, example)
                for rank, pattern, support, example in rows
            ),
        )

    def knn(
        self,
        node_type: str,
        query_vector: list[float],
        *,
        k: int,
        exclude_node_id: str | None = None,
    ) -> list[tuple[str, float]]:
        """Return the ``k`` nearest nodes of ``node_type`` via sqlite-vec MATCH."""
        table = _VEC_TABLES[node_type]
        limit = k + (1 if exclude_node_id else 0)
        rows = self._conn.execute(
            f"SELECT node_id, distance FROM {table} "
            f"WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (self._serialize(query_vector), limit),
        ).fetchall()
        hits = [
            (node_id, float(distance)) for node_id, distance in rows if node_id != exclude_node_id
        ]
        return hits[:k]

    def counts(self) -> dict[str, int]:
        """Return row counts for every node table plus edges and sequences."""
        result: dict[str, int] = {}
        for node_type, table in _VEC_TABLES.items():
            result[node_type] = self._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        result["edges"] = self._conn.execute("SELECT count(*) FROM edges").fetchone()[0]
        result["sequences"] = self._conn.execute("SELECT count(*) FROM sequences").fetchone()[0]
        return result

    def commit(self) -> None:
        """Commit the current transaction."""
        self._conn.commit()

    def close(self) -> None:
        """Commit and close the connection."""
        self._conn.commit()
        self._conn.close()
