"""Ingest a PDF: parse -> sections -> chunks -> embeddings -> LanceDB.

    python ingest.py path/to/file.pdf [--force] [--show N]
"""

import argparse
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from tutor import config  # noqa: E402
from tutor.library import DocumentError, ingest, open_document  # noqa: E402
from tutor.retrieval.embedder import Embedder  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--force", action="store_true", help="re-ingest even if already done (resets progress)")
    ap.add_argument("--show", type=int, default=3, help="sample chunks to print")
    args = ap.parse_args()
    if not args.pdf.is_file():
        print(f"Not a file: {args.pdf}")
        return 1

    started = time.perf_counter()
    embedder = Embedder()
    try:
        result = ingest(args.pdf, embedder, force=args.force)
    except DocumentError as e:
        print(f"Error: {e}")
        return 1

    if result.skipped:
        print(f"Already ingested (use --force to redo): {result.title}")
    print(f"\ndoc_id      {result.doc_id}\ntitle       {result.title}\npages       {result.pages}"
          f"\nsections    {result.sections} (from {result.structure_source})\nchunks      {result.chunks}"
          f"\nstored in   {config.doc_dir(result.doc_id)}\ntime        {time.perf_counter() - started:.1f}s")

    doc = open_document(result.doc_id, embedder)
    print("\nOutline:")
    for s in doc.lesson.outline(max_level=3)["sections"]:
        print(f"  {'  ' * (s['level'] - 1)}{s['id']}  {s['title']}  (p. {s['pages']})")

    chunks = doc.lesson.chunks
    if args.show and chunks:
        print("\nSample chunks:")
        step = max(1, len(chunks) // args.show)
        for c in chunks[::step][: args.show]:
            tokens = embedder.count_tokens(c.text)
            print(f"\n--- #{c.order}  p.{c.page_start}-{c.page_end}  {tokens} tok  [{c.heading_path}]")
            print(c.body[:400] + (" ..." if len(c.body) > 400 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
