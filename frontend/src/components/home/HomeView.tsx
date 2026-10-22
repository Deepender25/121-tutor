"use client";
// Home: the library. One visual system throughout: every book (continue reading, the shelf, the blank "new
// notebook" book) stands on the same plinth with the same caption style; sections share one heading style.
// Long-press (touch) or right-click a book for its menu. Dropping PDFs here makes a notebook for them.
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState, useTransition } from "react";
import { api } from "@/lib/api";
import { notebookCover } from "@/lib/covers";
import { greeting, pagesLabel, plain, plural, timeAgo } from "@/lib/format";
import type { NotebookSummary } from "@/lib/types";
import { Icon, Mark } from "../Icon";
import { confirmDialog, openMenu, promptDialog, toast, useHydrated } from "../overlays";
import { openSettings, StatusPill } from "../Settings";
import { Book } from "../ui/book";
import { pickCover } from "../ui/cover-picker";

const href = (nb: NotebookSummary) => `/notebooks/${nb.id}`;
const isPdf = (f: File) => f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf");
// A notebook named after the file, as run.bat does for a PDF dropped on it: "Organic_Chem.pdf" -> "Organic Chem".
const nameFor = (f: File) => f.name.replace(/\.[^.]*$/, "").replace(/_/g, " ").trim().slice(0, 120) || "New notebook";

function facts(nb: NotebookSummary) {
  const pages = nb.sources.reduce((n, s) => n + (s.pages || 0), 0);
  const active = nb.sources.find((s) => s.doc_id === nb.active_source) || nb.sources[0];
  const position = active?.position?.started ? active.position : null;
  return { pages, active, position };
}

// "5. The Semantic Web › 5.1 The Semantic Web stack › Memory Trick" -> where you are, and the section it's in.
function whereParts(section: string): { here: string; within: string } {
  const parts = plain(section).split(/\s*›\s*/).filter(Boolean);
  return { here: parts[parts.length - 1] || section, within: parts.length > 1 ? parts[parts.length - 2] : "" };
}

export function HomeView({ initialNotebooks }: { initialNotebooks: NotebookSummary[] }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const [notebooks, setNotebooks] = useState(initialNotebooks);
  const [query, setQuery] = useState("");
  // Opening a notebook loads it on the server first: the book that was clicked shows it's on its way.
  const [opening, startOpening] = useTransition();
  const [openingId, setOpeningId] = useState<string | null>(null);
  const adding = useRef(false);

  const refresh = useCallback(async () => {
    try { setNotebooks(await api.notebooks()); } catch { /* keep what we have */ }
  }, []);
  // Coming back with the browser's Back button can show a cached page: bring it up to date.
  useEffect(() => { refresh(); }, [refresh]);

  const open = (nb: NotebookSummary) => {
    setOpeningId(nb.id);
    startOpening(() => router.push(href(nb)));
  };

  // PDFs dropped anywhere on Home: a new notebook for them (instead of the browser leaving the app to show
  // the file). The notebook page picks the indexing jobs up from its first load.
  useEffect(() => {
    const hasFiles = (e: DragEvent) => [...(e.dataTransfer?.types || [])].includes("Files");
    const onDragOver = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    };
    const onDrop = async (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      const files = [...(e.dataTransfer?.files || [])];
      const pdfs = files.filter(isPdf);
      if (!pdfs.length) { toast("Only PDF files can be added.", "error"); return; }
      if (adding.current) return; // one at a time
      adding.current = true;
      if (pdfs.length < files.length) toast("Skipped files that aren't PDFs.", "info");
      const name = nameFor(pdfs[0]);
      try {
        const nb = await api.createNotebook(name);
        toast(`Adding “${name}”…`, "info");
        await api.upload(nb.id, pdfs);
        router.push(href(nb));
      } catch (err) {
        toast((err as Error).message, "error", 6000);
        refresh();
      } finally {
        adding.current = false;
      }
    };
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("drop", onDrop);
    };
  }, [router, refresh]);

  const create = async () => {
    const name = await promptDialog({ title: "New notebook", body: "Give it a name. You'll add PDFs next.", placeholder: "e.g. Organic Chemistry, Unit 3", confirm: "Create notebook" });
    if (!name) return;
    try {
      const nb = await api.createNotebook(name);
      router.push(href(nb));
    } catch (e) { toast((e as Error).message, "error"); }
  };

  const menu = (nb: NotebookSummary, anchor: Element) => openMenu(anchor, [
    { label: "Open", icon: "arrowRight", action: () => open(nb) },
    { label: "Cover colour", icon: "nb-palette", action: async () => {
      const cover = await pickCover(nb.name, notebookCover(nb).key);
      if (!cover) return;
      try { await api.updateNotebook(nb.id, { cover }); await refresh(); } catch (err) { toast((err as Error).message, "error"); }
    } },
    { label: "Rename", icon: "edit", action: async () => {
      const name = await promptDialog({ title: "Rename notebook", value: nb.name });
      if (!name) return;
      try { await api.updateNotebook(nb.id, { name }); await refresh(); toast("Renamed", "success"); } catch (err) { toast((err as Error).message, "error"); }
    } },
    { label: "Delete", icon: "trash", danger: true, action: async () => {
      const ok = await confirmDialog({ title: `Delete “${nb.name}”?`, body: "Its conversation history and any sources not used by another notebook will be removed. This can't be undone." });
      if (!ok) return;
      try { await api.deleteNotebook(nb.id); await refresh(); toast("Notebook deleted", "success"); } catch (err) { toast((err as Error).message, "error"); }
    } },
  ]);

  // A book on its plinth: the one building block of the page.
  const plinth = (nb: NotebookSummary, className = "") => (
    <div className={`shelf-book${className ? ` ${className}` : ""}${opening && openingId === nb.id ? " pending" : ""}`} role="link" tabIndex={0}
      aria-label={`Open ${nb.name}`} aria-busy={opening && openingId === nb.id ? true : undefined}
      onClick={() => open(nb)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(nb); } }}
      onContextMenu={(e) => { e.preventDefault(); menu(nb, e.currentTarget); }}>
      <Book title={nb.name} subtitle={nb.sources.length ? plural(nb.sources.length, "source") : "Empty"} cover={notebookCover(nb)} />
    </div>
  );

  const shelfItem = (nb: NotebookSummary) => {
    const { pages, position } = facts(nb);
    return (
      <article key={nb.id} className="shelf-item">
        {plinth(nb)}
        <div className="shelf-caption">
          <div className="shelf-text">
            <h3 title={nb.name}>{nb.name}</h3>
            <p>{nb.sources.length ? `${plural(nb.sources.length, "source")} · ${plural(pages, "page")}` : "No sources yet"}</p>
            <p className="faint">{position ? pagesLabel(position.pages) : nb.sources.length ? "Not started" : "Add a source to begin"}{hydrated ? ` · ${timeAgo(nb.updated_at)}` : ""}</p>
          </div>
          <button className="icon-btn xs card-menu" aria-label={`Options for ${nb.name}`} data-tip="Options"
            onClick={(e) => { e.stopPropagation(); menu(nb, e.currentTarget); }}>
            <Icon name="more" className="sm" />
          </button>
        </div>
      </article>
    );
  };

  const newItem = (
    <article className="shelf-item">
      <button className="shelf-book shelf-new" onClick={create} aria-label="New notebook">
        <span className="new-book" aria-hidden="true"><span className="plus" /></span>
      </button>
      <div className="shelf-caption">
        <div className="shelf-text">
          <h3>New notebook</h3>
          <p>One subject, its PDFs</p>
        </div>
      </div>
    </article>
  );

  const q = query.trim().toLowerCase();
  const shown = notebooks.filter((nb) => !q || nb.name.toLowerCase().includes(q) || nb.sources.some((s) => (s.title || "").toLowerCase().includes(q)));
  const totalSources = new Set(notebooks.flatMap((nb) => nb.sources.map((s) => s.doc_id))).size;
  const hello = hydrated ? greeting() : " "; // local time of day: only known in the browser

  // Continue reading: the most recently used notebook that has something to read.
  const recent = notebooks.find((nb) => nb.sources.length);
  let reading = null;
  if (recent) {
    const { active, position } = facts(recent);
    const where = position ? whereParts(position.section) : null;
    reading = (
      <section className="home-section">
        <div className="section-head"><h2>{position ? "Pick up where you left off" : "Start reading"}</h2></div>
        <div className="reading">
          {plinth(recent, "reading-plinth")}
          <div className="reading-text">
            <h3 className="reading-title">{recent.name}</h3>
            {where ? (
              <p className="reading-where">
                {where.within ? <span className="within">{where.within}</span> : null}
                <span className="here">{where.here}</span>
              </p>
            ) : <p className="reading-where"><span className="here">Not started yet</span></p>}
            <p className="reading-meta">
              {[position ? pagesLabel(position.pages) : null, recent.sources.length > 1 ? plain(active?.title) : null, hydrated ? timeAgo(recent.updated_at) : null]
                .filter(Boolean).join(" · ")}
            </p>
            <div className="reading-actions">
              <button className="btn primary" onClick={() => open(recent)}>{position ? "Resume" : "Start"}<Icon name="arrowRight" className="sm" /></button>
            </div>
          </div>
        </div>
      </section>
    );
  }

  const body = notebooks.length ? (
    <div className="home-inner">
      <header className="library-head">
        <div className="library-title">
          <span className="eyebrow">{hello}</span>
          <h1>Your library</h1>
          <p className="library-meta">{`${plural(notebooks.length, "notebook")} · ${plural(totalSources, "source")}`}</p>
        </div>
        <div className="library-actions">
          <label className="search">
            <Icon name="search" className="sm" />
            <input type="search" placeholder="Search" aria-label="Search notebooks and sources" value={query} onChange={(e) => setQuery(e.target.value)} />
          </label>
          <button className="btn primary" onClick={create}><Icon name="plus" className="sm" />New notebook</button>
        </div>
      </header>
      {q ? null : reading}
      <section className="home-section">
        <div className="section-head">
          <h2>{q ? `Results for “${query.trim()}”` : "Notebooks"}</h2>
          <span className="count">{q ? `${shown.length} of ${notebooks.length}` : notebooks.length}</span>
        </div>
        <div className="shelf">
          {shown.map(shelfItem)}
          {q ? null : newItem}
          {shown.length || !q ? null : <div className="no-results">{`Nothing matches “${query.trim()}”.`}</div>}
        </div>
      </section>
    </div>
  ) : (
    <div className="home-inner">
      <section className="library-empty">
        <div className="shelf-book empty-plinth" onClick={create} role="button" tabIndex={0} aria-label="Create your first notebook"
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); create(); } }}>
          <Book title="Your first notebook" subtitle="Add a PDF" />
        </div>
        <div className="empty-text">
          <span className="eyebrow">{hello}</span>
          <h1>Start your library</h1>
          <p>Each notebook holds the PDFs for one subject. Add one, then talk it through: lessons in order, answers with page references, and the web when your notes run out.</p>
          <button className="btn primary" onClick={create}><Icon name="plus" className="sm" />New notebook</button>
          <dl className="capabilities">
            <div><dt>Tutor</dt><dd>Teaches your PDF in order and remembers where you stopped.</dd></div>
            <div><dt>Explainer</dt><dd>Answers from your sources, with the page it came from.</dd></div>
            <div><dt>Researcher</dt><dd>Checks the web and says which answer came from where.</dd></div>
            <div><dt>Brainstormer</dt><dd>Builds new ideas on top of your material.</dd></div>
          </dl>
        </div>
      </section>
    </div>
  );

  return (
    <div className="home">
      <header className="topbar">
        <Link className="brand" href="/"><Mark className="brand-mark" /><span>Voice Tutor</span></Link>
        <div className="spacer" />
        <div className="topbar-actions">
          <StatusPill />
          <button className="icon-btn" aria-label="Settings" data-tip="Settings" onClick={() => openSettings()}><Icon name="settings" /></button>
        </div>
      </header>
      <main className="home-main scroll">{body}</main>
    </div>
  );
}
