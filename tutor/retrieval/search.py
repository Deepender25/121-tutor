"""Hybrid search across one or more document tables, fused with a single global RRF.

Per-table hybrid scores are rank-based, so merging them would make every source's best chunk look
equally relevant. Instead we pool vector hits from all tables (cosine distances are comparable: same
embedding model) and full-text hits (BM25), rank each list globally, then fuse once.

Vector hits only ever come from tables in the query vector's embedding space (the caller passes those as
vector_stores); a document without one yet (being re-embedded after a mode switch), or a query that couldn't be
embedded (query_vector=None), still gets full-text hits.
"""

import re

import numpy as np

from tutor.document import strip_tags

_FTS_UNSAFE = re.compile(r"[^\w\s.\-]+")
RRF_K = 60


def _row(doc_id: str, r: dict) -> dict:
    return {
        "doc_id": doc_id,
        "order": int(r["order"]),
        "section_id": r["section_id"],
        "heading_path": strip_tags(r["heading_path"]),
        "page_start": int(r["page_start"]),
        "page_end": int(r["page_end"]),
        "text": r["text"],
        "overlap_chars": int(r.get("overlap_chars") or 0),  # leading text copied from the previous chunk
    }


def hybrid_search(stores: dict, query: str, query_vector: np.ndarray | None, top_k: int = 5,
                  vector_stores: dict | None = None) -> list[dict]:
    """stores: {doc_id: ChunkStore} for full-text search; vector_stores: the ones in query_vector's embedding space
    (default: all of `stores`). Returns top_k chunks (each with doc_id and a fused `score`)."""
    pool = max(top_k * 4, 20)
    text = _FTS_UNSAFE.sub(" ", query).strip()
    vector_hits: list[tuple[float, dict]] = []
    text_hits: list[tuple[float, dict]] = []
    if query_vector is not None:
        vec = query_vector.tolist()
        for doc_id, store in (stores if vector_stores is None else vector_stores).items():
            for r in store.table.search(vec, query_type="vector").limit(pool).to_list():
                vector_hits.append((r["_distance"], _row(doc_id, r)))
    for doc_id, store in stores.items():
        table = store.table
        if text:
            try:
                for r in table.search(text, query_type="fts").limit(pool).to_list():
                    text_hits.append((r["_score"], _row(doc_id, r)))
            except Exception:  # query that the FTS parser rejects: vector results still stand
                pass

    fused: dict[tuple[str, int], dict] = {}
    vector_hits.sort(key=lambda x: x[0])  # smaller distance = better
    text_hits.sort(key=lambda x: -x[0])  # larger BM25 = better
    for ranked in (vector_hits, text_hits):
        for rank, (_, row) in enumerate(ranked[:pool]):
            key = (row["doc_id"], row["order"])
            entry = fused.setdefault(key, {**row, "score": 0.0})
            entry["score"] += 1.0 / (RRF_K + rank + 1)
    return sorted(fused.values(), key=lambda r: -r["score"])[:top_k]
