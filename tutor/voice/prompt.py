"""Build the system instruction for a session from the notebook's live state.

Position and history are injected here on every (re)connect because the model's own context gets
compressed and resumption handles expire.
"""

from tutor import config
from tutor.library import DocumentCache
from tutor.notebooks import Notebook

TEMPLATE_PATH = config.PROJECT_ROOT / "prompts" / "system.md"
MAX_OUTLINE_LINES = 40
RECENT_TURNS = 16
RECENT_CHARS = 6000


def _sources_block(nb: Notebook, cache: DocumentCache) -> str:
    if not nb.sources:
        return ("This notebook has no sources yet. Tell the person they can add a PDF with the 'Add source' "
                "button. Meanwhile you can still chat, brainstorm and research on the web, but say clearly "
                "that answers aren't from their material.")
    lines = []
    for n, doc_id in enumerate(nb.sources, 1):
        lesson = cache.get(doc_id).lesson
        active = doc_id == nb.active_source
        outline = lesson.outline(max_level=2 if active else 1)
        tag = " [ACTIVE: the one being taught]" if active else ""
        lines.append(f"{n}. \"{lesson.title}\", {outline['pages']} pages, {outline['document_covered']} covered{tag}")
        shown = outline["sections"][:MAX_OUTLINE_LINES if active else 12]
        for s in shown:
            indent = "   " * s["level"]
            done = {"completed": " (completed)", "in_progress": " (in progress)"}.get(s["status"], "")
            lines.append(f"{indent}- {s['title']} (p. {s['pages']}, id {s['id']}){done}")
        if len(outline["sections"]) > len(shown):
            lines.append("   ... more sections: call get_outline for the full list")
    return "\n".join(lines)


def _position_block(nb: Notebook, cache: DocumentCache) -> str:
    if not nb.active_source:
        return ""
    lesson = cache.get(nb.active_source).lesson
    pos = lesson.position()
    if not pos.get("started"):
        return (f"# Reading position\nThe active source \"{lesson.title}\" hasn't been started yet. If the person "
                "wants to be taught, begin with read_passage target start.")
    return (f"# Reading position\nIn \"{lesson.title}\": {pos['section']}, page {pos['pages']}, part {pos['part']}. "
            f"\"Continue\" means read_passage target next from here.")


def _recent_block(nb: Notebook) -> str:
    turns = [h for h in nb.history if h.get("role") in ("user", "model")][-RECENT_TURNS:]
    if not turns:
        return ""
    lines, used = [], 0
    for h in reversed(turns):
        line = f"{'Person' if h['role'] == 'user' else 'You'}: {h['text'].strip()}"
        if used + len(line) > RECENT_CHARS:
            break
        lines.insert(0, line)
        used += len(line)
    return ("# Recent conversation (from before this connection; continue naturally, don't repeat it)\n"
            + "\n".join(lines))


def build_system_prompt(nb: Notebook, cache: DocumentCache) -> str:
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    for key, value in {
        "{notebook_name}": nb.name,
        "{sources_block}": _sources_block(nb, cache),
        "{position_block}": _position_block(nb, cache),
        "{recent_block}": _recent_block(nb),
    }.items():
        text = text.replace(key, value)
    return text.strip() + "\n"
