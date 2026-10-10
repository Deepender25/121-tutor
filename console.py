"""Text console over one document, for debugging retrieval and the lesson cursor without voice.
Drives the same tools the voice model calls. (The real app is `python app.py` / run.bat.)

    python console.py [file.pdf]
"""

import sys
import textwrap
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from tutor.library import DocumentCache, DocumentError, StaleIndexError, ingest, resolve_doc_id  # noqa: E402

HELP = """Commands:
  next | n            continue to the next part        back | b      previous part
  repeat | r          show the current part again      start         go to the beginning
  go <target>         jump: "go chapter 3", "go 3.1", "go page 12", "go s004"
  ask <question>      search the document              outline | o   sections + progress
  where | w           current position                 load <pdf>    switch document
  help | h            this list                        quit | q      exit
Anything else you type is treated as a question (ask)."""


def ask_for_pdf() -> Path | None:
    print("No document loaded yet.")
    while True:
        raw = input("Drag a PDF into this window (or type its path) and press Enter, or 'q' to quit: ").strip()
        if raw.lower() in ("q", "quit", "exit"):
            return None
        path = Path(raw.strip('"').strip("'"))
        if path.is_file() and path.suffix.lower() == ".pdf":
            return path
        print(f"  Not a PDF file: {path}")


def load(pdf: Path, embedder) -> str:
    result = ingest(pdf, embedder)
    if result.skipped:
        print(f"Loaded (already indexed): {result.title}")
    else:
        print(f"Indexed {result.title}: {result.pages} pages, {result.sections} sections, {result.chunks} chunks")
    return result.doc_id


def show_passage(p: dict) -> None:
    if "error" in p:
        print(f"  ! {p['error']}")
        return
    print(f"\n[{p['section']} · p.{p['pages']} · {p['position']} · {p['document_covered']} covered]\n")
    for para in p["text"].split("\n\n"):
        print(textwrap.fill(para, width=100))
        print()
    if p["is_document_end"]:
        print("  (end of document)")
    elif p["is_section_end"]:
        print("  (end of section)")


def show_outline(o: dict) -> None:
    print(f"\n{o['title']} — {o['pages']} pages — {o['document_covered']} covered")
    for s in o["sections"]:
        mark = "x" if s["covered"] else " "
        print(f"  [{mark}] {'   ' * (s['level'] - 1)}{s['title']}  (p.{s['pages']}, {s['id']})")
    show_where(o["position"])


def show_where(pos: dict) -> None:
    if not pos.get("started"):
        print("  Not started yet. Type 'next' to begin.")
    else:
        print(f"  Position: {pos['section']}, p.{pos['pages']}, part {pos['part']}")


def show_results(r: dict) -> None:
    if "error" in r:
        print(f"  ! {r['error']}")
        return
    if not r.get("results"):
        print("  Nothing in the document matched.")
        return
    for i, hit in enumerate(r["results"], 1):
        snippet = " ".join(hit["text"].split())[:300]
        print(f"\n  {i}. [{hit['section']} · p.{hit['pages']}]\n     {snippet}...")


def console(tools, lesson) -> None:
    def call(name: str, args: dict) -> dict:
        return tools.call(name, args).result

    print(f"\n=== {lesson.title} ===  (debug console; type help for commands)")
    show_where(lesson.position())
    if lesson.progress.last_summary:
        print(f"  {lesson.progress.last_summary}")
    while True:
        try:
            line = input("\n> ").lstrip("﻿").strip()  # piped input from PowerShell can start with a BOM
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        cmd, _, arg = line.partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        if cmd in ("q", "quit", "exit"):
            return
        elif cmd in ("h", "help", "?"):
            print(HELP)
        elif cmd in ("n", "next", "continue"):
            show_passage(call("read_passage", {"target": "next"}))
        elif cmd in ("b", "back", "previous"):
            show_passage(call("read_passage", {"target": "previous"}))
        elif cmd in ("r", "repeat", "again"):
            show_passage(call("read_passage", {"target": "current"}))
        elif cmd in ("start", "beginning"):
            show_passage(call("read_passage", {"target": "start"}))
        elif cmd in ("go", "goto", "jump"):
            target = arg.replace("page ", "page:") if arg.lower().startswith("page ") else arg
            show_passage(call("read_passage", {"target": target}) if target else {"error": "go where?"})
        elif cmd in ("o", "outline"):
            show_outline(call("get_outline", {})["outline"])
        elif cmd in ("w", "where"):
            show_where(lesson.position())
        elif cmd == "load":
            raise SwitchDocument(Path(arg.strip('"')))
        elif cmd in ("ask", "search", "s"):
            if arg:
                show_results(call("search_sources", {"query": arg}))
            else:
                print("  ask what?")
        else:
            show_results(call("search_sources", {"query": line}))


class SwitchDocument(Exception):
    def __init__(self, pdf: Path):
        self.pdf = pdf


def main() -> int:
    print("Loading the embedding model ...")
    from tutor.retrieval.embedder import Embedder
    from tutor.tools import NotebookTools

    embedder = Embedder()
    cache = DocumentCache(embedder)
    pdf = Path(sys.argv[1].strip('"')) if len(sys.argv) > 1 else None

    while True:
        try:
            if pdf is not None:
                if not pdf.is_file():
                    print(f"Not a file: {pdf}")
                    pdf = None
                    continue
                doc_ref = load(pdf, embedder)
            else:
                try:
                    doc_ref = resolve_doc_id(None)
                except DocumentError:
                    pdf = ask_for_pdf()
                    if pdf is None:
                        return 0
                    continue
            cache.drop(doc_ref)
            doc = cache.get(doc_ref)
        except StaleIndexError as e:
            if e.source_file.is_file():
                print("Settings changed since this document was indexed; re-indexing ...")
                ingest(e.source_file, embedder, force=True)
                pdf = None
                continue
            print(f"Error: {e}\nThe original file {e.source_file} is gone; please load it again.")
            pdf = ask_for_pdf()
            if pdf is None:
                return 1
            continue
        except DocumentError as e:
            print(f"Error: {e}")
            pdf = ask_for_pdf()
            if pdf is None:
                return 1
            continue

        try:
            console(NotebookTools(cache, embedder, [doc.doc_id], doc.doc_id), doc.lesson)
            return 0
        except SwitchDocument as switch:
            pdf = switch.pdf


if __name__ == "__main__":
    sys.exit(main())
