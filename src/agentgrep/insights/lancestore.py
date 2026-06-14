"""Optional LanceDB vector backend for the insights graph engine.

The default graph store (:mod:`agentgrep.insights.graphstore`) keeps vectors in
a ``sqlite-vec`` ``vec0`` table, which is **brute-force** — fine for the small
prompt graphs a single user produces, but O(N) per query. When the optional
``lancedb`` package is installed and the graph is large, this backend holds the
node vectors in a Lance table and builds a true **IVF-PQ** ANN index, so the
similar-edge graph is built with sub-linear nearest-neighbor queries.

It owns only the *vectors* and their kNN; the relational graph
(nodes/edges/sequences/summaries) stays in the sqlite ``GraphStore``. The
backend is build-once per run — the engine already holds every vector in
memory.
"""

from __future__ import annotations

import typing as t

if t.TYPE_CHECKING:
    import pathlib

    from agentgrep.insights.loader import ImportModule

# Below this many vectors an IVF-PQ index costs more to train than brute force
# saves, so the table is left index-less (LanceDB still answers exactly).
_MIN_IVFPQ_ROWS = 4096


class LanceVectorBackend:
    """A LanceDB-backed vector store exposing the graph engine's kNN call."""

    def __init__(self, table: t.Any, *, distance: str, indexed: bool) -> None:
        self._table = table
        self._distance = distance
        self.indexed = indexed

    @classmethod
    def build(
        cls,
        path: pathlib.Path,
        node_ids: list[str],
        matrix: t.Any,
        *,
        import_module: ImportModule,
        table_name: str = "vectors",
        distance: str = "cosine",
    ) -> LanceVectorBackend:
        """Create a Lance table from all vectors, indexing it past the threshold.

        Parameters
        ----------
        path : pathlib.Path
            LanceDB database directory.
        node_ids : list[str]
            Node id per matrix row.
        matrix : numpy.ndarray
            Row-aligned float32 vectors.
        import_module : ImportModule
            Injectable importer (resolves ``lancedb``).
        """
        lancedb = import_module("lancedb")
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = lancedb.connect(str(path))
        rows = [
            {"node_id": node_id, "vector": matrix[index].tolist()}
            for index, node_id in enumerate(node_ids)
        ]
        table = connection.create_table(table_name, data=rows, mode="overwrite")
        indexed = False
        if len(rows) >= _MIN_IVFPQ_ROWS:
            index_module = import_module("lancedb.index")
            table.create_index(
                "vector",
                config=index_module.IvfPq(distance_type=distance),
                replace=True,
            )
            indexed = True
        return cls(table, distance=distance, indexed=indexed)

    def knn(self, query_vector: list[float], k: int) -> list[tuple[str, float]]:
        """Return the ``k`` nearest ``(node_id, distance)`` pairs (cosine distance)."""
        results = self._table.search(query_vector).distance_type(self._distance).limit(k).to_list()
        return [(str(row["node_id"]), float(row["_distance"])) for row in results]
