"""LanceDB chunk table: vectors + native full-text index. Search logic lives in search.py."""

from pathlib import Path

import lancedb
import numpy as np
import pyarrow as pa
from lancedb.index import FTS

from tutor.document import Chunk, strip_tags

TABLE = "chunks"


def _schema(dim: int) -> pa.Schema:
    return pa.schema([
        pa.field("chunk_id", pa.string()),
        pa.field("order", pa.int32()),
        pa.field("section_id", pa.string()),
        pa.field("heading_path", pa.string()),
        pa.field("page_start", pa.int32()),
        pa.field("page_end", pa.int32()),
        pa.field("text", pa.string()),
        pa.field("search_text", pa.string()),  # heading path + text: lets "section 3.1" hit the heading
        pa.field("overlap_chars", pa.int32()),
        pa.field("vector", pa.list_(pa.float32(), dim)),
    ])


class ChunkStore:
    def __init__(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)
        self.db = lancedb.connect(str(path))
        self._table = None

    @property
    def table(self):
        if self._table is None:
            self._table = self.db.open_table(TABLE)
        return self._table

    def write(self, doc_id: str, chunks: list[Chunk], vectors: np.ndarray) -> None:
        rows = [
            {
                "chunk_id": f"{doc_id}:{c.order}",
                "order": c.order,
                "section_id": c.section_id,
                "heading_path": c.heading_path,
                "page_start": c.page_start,
                "page_end": c.page_end,
                "text": c.text,
                "search_text": f"{c.heading_path}\n\n{c.text}",
                "overlap_chars": c.overlap_chars,
                "vector": v.tolist(),
            }
            for c, v in zip(chunks, vectors, strict=True)
        ]
        self._table = self.db.create_table(TABLE, data=rows, schema=_schema(vectors.shape[1]), mode="overwrite")
        self._table.create_index(
            "search_text",  # unified API: column first
            config=FTS(language="English", stem=True, remove_stop_words=True, ascii_folding=True),
            replace=True,
        )
        self._table.optimize()

    def load_chunks(self) -> list[Chunk]:
        """All chunks in reading order, without vectors."""
        df = self.table.to_arrow().drop_columns(["vector", "search_text"]).to_pandas().sort_values("order")
        return [
            Chunk(int(r.order), r.section_id, strip_tags(r.heading_path), int(r.page_start), int(r.page_end), r.text,
                  int(r.overlap_chars))
            for r in df.itertuples()
        ]

    def search(self, query: str, query_vector: np.ndarray, top_k: int = 5) -> list[dict]:
        from tutor.retrieval.search import hybrid_search

        return hybrid_search({"": self}, query, query_vector, top_k)
