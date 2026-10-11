"use client";
// Home: greeting hero, "continue where you left off", notebook grid with search.
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { greeting, timeAgo } from "@/lib/format";
import type { NotebookSummary } from "@/lib/types";
import { Icon, Mark, NotebookIcon } from "../Icon";
import { confirmDialog, openMenu, promptDialog, toast, useHydrated } from "../overlays";
import { openSettings, StatusPill } from "../Settings";

function stats(nb: NotebookSummary) {
  const pages = nb.sources.reduce((n, s) => n + (s.pages || 0), 0);
  const active = nb.sources.find((s) => s.doc_id === nb.active_source) || nb.sources[0];
  const at = active?.position?.started ? `At p. ${active.position.pages}` : "Not started";
  return { pages, at };
}

const href = (nb: NotebookSummary) => `/notebooks/${nb.id}`;

export function HomeView({ initialNotebooks }: { initialNotebooks: NotebookSummary[] }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const [notebooks, setNotebooks] = useState(initialNotebooks);
  const [query, setQuery] = useState("");

  const refresh = useCallback(async () => {
    try { setNotebooks(await api.notebooks()); } catch { /* keep what we have */ }
  }, []);
  // Coming back with the browser's Back button can show a cached page: bring progress up to date.
  useEffect(() => { refresh(); }, [refresh]);

  const open = (nb: NotebookSummary) => router.push(href(nb));

  const create = async () => {
    const name = await promptDialog({ title: "New notebook", body: "Give it a name. You'll add PDFs next.", placeholder: "e.g. Organic Chemistry, Unit 3", confirm: "Create notebook" });
    if (!name) return;
    try {
      const nb = await api.createNotebook(name);
      router.push(href(nb));
    } catch (e) { toast((e as Error).message, "error"); }
  };

  const cardMenu = (nb: NotebookSummary) => (
    <button className="icon-btn sm card-menu" aria-label={`Options for ${nb.name}`}
      onClick={(e) => {
        e.stopPropagation();
        openMenu(e.currentTarget, [
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
      }}>
      <Icon name="more" />
    </button>
  );

  const card = (nb: NotebookSummary) => {
    const { pages, at } = stats(nb);
    return (
      <article key={nb.id} className="nb-card" role="link" tabIndex={0}
        onClick={() => open(nb)} onKeyDown={(e) => { if (e.key === "Enter") open(nb); }}>
        <div className="card-top"><span className="icon-tile"><NotebookIcon name={nb.icon} /></span>{cardMenu(nb)}</div>
        <div className="card-body">
          <h3>{nb.name}</h3>
          <div className="chips">
            <span className="chip"><Icon name="file" className="sm" />{nb.sources.length ? `${nb.sources.length} source${nb.sources.length > 1 ? "s" : ""}` : "No sources"}</span>
            {pages ? <span className="chip"><Icon name="layers" className="sm" />{`${pages} pages`}</span> : null}
          </div>
        </div>
        <div className="card-foot">
          {nb.sources.length
            ? <span className="progress"><Icon name="book" className="sm" />{at}</span>
            : <span className="progress muted">Add a PDF to start</span>}
          <span className="when">{hydrated ? timeAgo(nb.updated_at) : ""}</span>
        </div>
      </article>
    );
  };

  const q = query.trim().toLowerCase();
  const shown = notebooks.filter((nb) => !q || nb.name.toLowerCase().includes(q) || nb.sources.some((s) => (s.title || "").toLowerCase().includes(q)));
  const count = q ? `${shown.length} of ${notebooks.length}` : `${notebooks.length}`;

  const recent = notebooks.find((nb) => nb.sources.length);
  let continueCard = null;
  if (recent) {
    const active = recent.sources.find((s) => s.doc_id === recent.active_source) || recent.sources[0];
    const where = active?.position?.started ? `${active.position.section} · p. ${active.position.pages}` : `Not started · ${active?.title || ""}`;
    continueCard = (
      <section className="continue" onClick={() => open(recent)} role="link" tabIndex={0}
        onKeyDown={(e) => { if (e.key === "Enter") open(recent); }}>
        <span className="icon-tile lg"><NotebookIcon name={recent.icon} /></span>
        <div className="continue-text">
          <span className="eyebrow">Continue where you left off</span>
          <h2>{recent.name}</h2>
          <p>{where}</p>
        </div>
        <button className="btn primary" onClick={(e) => { e.stopPropagation(); open(recent); }}>Resume<Icon name="arrowRight" className="sm" /></button>
      </section>
    );
  }

  const hello = hydrated ? greeting() : " "; // local time of day: only known in the browser
  const heroMark = <div className="hero-mark"><Mark /></div>;
  const body = notebooks.length ? (
    <div className="home-inner">
      <section className="hero">
        <div className="hero-text">
          <span className="eyebrow">{hello}</span>
          <h1>What shall we learn today?</h1>
          <p>Open a notebook and just talk. Your tutor teaches, explains, researches and brainstorms, and always says what&apos;s from your sources and what&apos;s from the web.</p>
          <div className="hero-actions"><button className="btn primary lg" onClick={create}><Icon name="plus" />New notebook</button></div>
        </div>
        {heroMark}
      </section>
      {continueCard}
      <div className="section-bar">
        <h2>Notebooks <span className="count">{count}</span></h2>
        <label className="search"><Icon name="search" className="sm" />
          <input type="search" placeholder="Search notebooks" aria-label="Search notebooks" value={query} onChange={(e) => setQuery(e.target.value)} />
        </label>
      </div>
      <div className="nb-grid">
        {shown.map(card)}
        {shown.length ? null : <div className="no-results">{`No notebooks match “${query}”.`}</div>}
      </div>
    </div>
  ) : (
    <div className="home-inner">
      <section className="welcome">
        {heroMark}
        <span className="eyebrow">{hello}</span>
        <h1>Talk to your documents</h1>
        <p>Create a notebook, drop in a PDF, and have a real conversation about it: step-by-step lessons, answers with page references, web research and brainstorming.</p>
        <button className="btn primary lg" onClick={create}><Icon name="plus" />Create your first notebook</button>
        <div className="feature-row">
          <Feature ic="book" title="Tutor" text="Teaches your PDF in order and remembers where you stopped." />
          <Feature ic="search" title="Explainer" text="Answers from your sources, with page references." />
          <Feature ic="globe" title="Researcher" text="Checks the web and tells you which is which." />
          <Feature ic="lightbulb" title="Brainstormer" text="Builds new ideas on top of your material." />
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

function Feature({ ic, title, text }: { ic: string; title: string; text: string }) {
  return (
    <div className="feature"><span className="feature-ic"><Icon name={ic} /></span><b>{title}</b><span>{text}</span></div>
  );
}
