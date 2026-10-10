"""Retrieval and lesson debugging CLI. Calls the same tool functions the voice model uses.

    python query.py "how big should the tank be?"         hybrid search (default: last ingested doc)
    python query.py --read next | start | s003 | page:4   move the reading cursor
    python query.py --outline
    python query.py --eval tests/questions.yaml           hit-rate on an eval set
"""

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from tutor.library import DocumentCache, DocumentError, resolve_doc_id  # noqa: E402
from tutor.retrieval.embedder import Embedder  # noqa: E402
from tutor.tools import NotebookTools  # noqa: E402


def run_eval(tools: NotebookTools, path: Path, top_k: int) -> int:
    import yaml

    cases = yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]
    hits = 0
    for case in cases:
        results = tools.search_sources(case["q"], top_k=top_k).result["results"]
        # A hit = some result contains the expected phrase (or, when none is given, covers an expected page).
        ok = False
        for r in results:
            if case.get("contains"):
                ok = case["contains"].lower() in r["text"].lower()
            else:
                start, _, end = r["pages"].partition("-")
                ok = any(int(start) <= p <= int(end or start) for p in case.get("pages", []))
            if ok:
                break
        hits += ok
        print(f"{'HIT ' if ok else 'MISS'}  {case['q']}  -> {[r['pages'] for r in results]}")
    print(f"\n{hits}/{len(cases)} hits at top-{top_k}")
    return 0 if hits == len(cases) else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="?")
    ap.add_argument("--doc", help="doc id or PDF path (default: last ingested)")
    ap.add_argument("--read", metavar="TARGET")
    ap.add_argument("--outline", action="store_true")
    ap.add_argument("--eval", type=Path)
    ap.add_argument("-k", type=int, default=5)
    args = ap.parse_args()

    embedder = Embedder()
    try:
        doc_id = resolve_doc_id(args.doc)
        cache = DocumentCache(embedder)
        cache.get(doc_id)
    except DocumentError as e:
        print(f"Error: {e}")
        return 1
    tools = NotebookTools(cache, embedder, [doc_id], doc_id)

    if args.eval:
        return run_eval(tools, args.eval, args.k)
    if args.outline:
        result = tools.call("get_outline", {}).result
    elif args.read:
        result = tools.call("read_passage", {"target": args.read}).result
    elif args.query:
        result = tools.call("search_sources", {"query": args.query, "top_k": args.k}).result
    else:
        ap.print_help()
        return 1
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
