"use client";
// Notebook page: top bar | sources + outline | conversation + voice | PDF viewer, plus reading mode's
// floating tutor. Rendering only: behaviour lives in the controller (controller.ts).
import Link from "next/link";
import { memo, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { clockTime, dayLabel, plain, plural } from "@/lib/format";
import { statusStore } from "@/lib/status";
import { useStore } from "@/lib/store";
import type { Notebook, Source, ToolUI } from "@/lib/types";
import { coverFor, notebookCover } from "@/lib/covers";
import { Icon, Mark } from "../Icon";
import { CoverChip } from "../ui/book";
import { pickCover } from "../ui/cover-picker";
import { copyText, useHydrated } from "../overlays";
import { KeyHelp, openSettings, StatusPill } from "../Settings";
import { type Act, createNotebookController, jobStageText, type Msg, type NotebookController, QUICK, type Tab } from "./controller";

type Ctl = NotebookController;
const cls = (...names: (string | false | null | undefined)[]) => names.filter(Boolean).join(" ");

export function NotebookView({ initial }: { initial: Notebook }) {
  const [ctl] = useState(() => createNotebookController(initial));
  useEffect(() => {
    ctl.mount();
    return () => ctl.dispose();
  }, [ctl]);
  const reading = useStore(ctl.store, (s) => s.reading);
  const idle = useStore(ctl.store, (s) => s.idle);
  const resizing = useStore(ctl.store, (s) => s.layout.resizing);

  return (
    <div className={cls("nb", reading && "reading", idle && "idle", resizing && "resizing")} ref={ctl.ref("nb")}>
      <Topbar ctl={ctl} />
      <Body ctl={ctl} />
      <FloatDock ctl={ctl} />
      <MobileTabs ctl={ctl} />
      <DropOverlay ctl={ctl} />
      <input type="file" accept="application/pdf,.pdf" multiple className="sr-only" ref={ctl.ref("fileInput")}
        onChange={(e) => { const files = [...(e.target.files || [])]; if (files.length) ctl.upload(files); e.target.value = ""; }} />
    </div>
  );
}

// ================================================================ top bar

function Topbar({ ctl }: { ctl: Ctl }) {
  const nb = useStore(ctl.store, (s) => s.nb);
  const voice = useStore(ctl.store, (s) => s.voice);
  const toggleActive = useStore(ctl.store, (s) => s.layout.toggleActive);
  const [name, setName] = useState(nb.name);
  const saved = useRef(nb.name);
  useEffect(() => { saved.current = nb.name; setName(nb.name); }, [nb.name]);

  const pages = nb.sources.reduce((n, s) => n + (s.pages || 0), 0);
  const sub = nb.sources.length ? `${plural(nb.sources.length, "source")} · ${plural(pages, "page")}` : "No sources yet";

  const commit = async () => {
    const next = name.trim();
    if (!next || next === saved.current) { setName(saved.current); return; }
    if (!(await ctl.rename(next))) setName(saved.current);
  };

  return (
    <header className="topbar">
      <Link className="icon-btn" aria-label="All notebooks" data-tip="All notebooks" href="/"><Icon name="back" /></Link>
      <div className="nb-title">
        <button className="nb-cover-btn" aria-label="Cover colour" data-tip="Cover colour"
          onClick={async () => { const c = await pickCover(nb.name, notebookCover(nb).key); if (c) ctl.setCover(c); }}>
          <CoverChip cover={notebookCover(nb)} className="nb-cover" />
        </button>
        <div className="nb-title-text">
          <input className="nb-name" value={name} aria-label="Notebook name" maxLength={120} spellCheck={false}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") e.currentTarget.blur();
              if (e.key === "Escape") { setName(saved.current); const el = e.currentTarget; requestAnimationFrame(() => el.blur()); }
            }}
            onBlur={commit} />
          <div className="nb-sub">{sub}</div>
        </div>
      </div>
      <div className="spacer" />
      <div className="topbar-actions">
        {voice.active ? <span className={`live-pill state-${voice.key}`}><span className="dot" />Live</span> : null}
        <StatusPill />
        <button className={cls("icon-btn", toggleActive && "active")} aria-label="Show or hide the page viewer" aria-pressed={toggleActive} data-tip="Page viewer" onClick={ctl.toggleViewer}><Icon name="panel" /></button>
        <button className="icon-btn" aria-label="Settings" data-tip="Settings" onClick={() => openSettings()}><Icon name="settings" /></button>
      </div>
    </header>
  );
}

// ================================================================ body: panels and splits

function Body({ ctl }: { ctl: Ctl }) {
  const l = useStore(ctl.store, (s) => s.layout);
  return (
    <div className={cls("nb-body", l.viewerHidden && "viewer-hidden", l.viewerOpen && "viewer-open", l.collapsed && "sources-collapsed")}
      data-tab={l.tab} ref={ctl.ref("body")}>
      <SourcesPanel ctl={ctl} />
      <Gutter ctl={ctl} which="sources" dragging={l.dragging === "sources"} />
      <ChatPanel ctl={ctl} />
      <Gutter ctl={ctl} which="viewer" dragging={l.dragging === "viewer"} />
      <ViewerPanel ctl={ctl} />
    </div>
  );
}

function Gutter({ ctl, which, dragging }: { ctl: Ctl; which: "sources" | "viewer"; dragging: boolean }) {
  return (
    <div className={cls("gutter", `g-${which}`, dragging && "dragging")} role="separator" tabIndex={0} aria-orientation="vertical"
      aria-label={which === "sources" ? "Resize the sources panel" : "Resize the page viewer"} title="Drag to resize · double-click to reset"
      ref={ctl.ref(which === "sources" ? "gutterSources" : "gutterViewer")}
      onPointerDown={(e) => ctl.gutterPointerDown(which, e)} onDoubleClick={() => ctl.resetSplit(which)} onKeyDown={(e) => ctl.gutterKeyDown(which, e)}>
      <i />
    </div>
  );
}

// ================================================================ sources & outline

const SourcesPanel = memo(function SourcesPanel({ ctl }: { ctl: Ctl }) {
  const nb = useStore(ctl.store, (s) => s.nb);
  const jobs = useStore(ctl.store, (s) => s.jobs);
  return (
    <section className="panel sources" aria-label="Sources">
      <div className="panel-head">
        <h2>Sources<span className="count-badge">{nb.sources.length ? String(nb.sources.length) : ""}</span></h2>
        <button className="btn sm" onClick={ctl.pickFiles}><Icon name="plus" className="sm" />Add</button>
      </div>
      <div className="panel-body scroll sources-body">
        <div className="sources-list">
          {nb.sources.map((s, i) => <SourceItem key={s.doc_id} ctl={ctl} s={s} index={i} active={s.doc_id === nb.active_source} many={nb.sources.length > 1} />)}
          {jobs.map((j) => {
            const failed = j.status === "error";
            return (
              <div key={j.id} className={`source job${failed ? " error" : ""}`}>
                <div className="file-tile"><Icon name={failed ? "alert" : j.stage === "uploading" ? "upload" : "loader"} className={failed || j.stage === "uploading" ? "" : "spin"} /></div>
                <div className="source-text">
                  <div className="title">{j.file_name}</div>
                  <div className="sub">{failed ? j.error : jobStageText(j)}</div>
                  {failed ? null : (
                    <div className={`job-bar${j.stage === "queued" || j.stage === "parsing" || (j.stage === "uploading" && j.percent >= 100) ? " indeterminate" : ""}`}><i style={{ width: `${j.percent}%` }} /></div>
                  )}
                </div>
                {failed ? <button className="icon-btn xs" aria-label="Dismiss" onClick={() => ctl.dismissJob(j.id)}><Icon name="x" className="sm" /></button> : null}
              </div>
            );
          })}
          {nb.sources.length || jobs.length ? null : (
            <button className="add-source" onClick={ctl.pickFiles}>
              <span className="add-ic"><Icon name="upload" /></span>
              <span><b>Add a PDF</b>Click, or drop files anywhere</span>
            </button>
          )}
        </div>
        <OutlineBox ctl={ctl} />
      </div>
    </section>
  );
});

function SourceItem({ ctl, s, index, active, many }: { ctl: Ctl; s: Source; index: number; active: boolean; many: boolean }) {
  return (
    <div className={`source${active ? " active" : ""}`} role="button" tabIndex={0}
      title={active ? "This source is being taught" : "Switch to this source"}
      onClick={() => ctl.setActive(s.doc_id)}
      onKeyDown={(e) => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); ctl.setActive(s.doc_id); } }}>
      <CoverChip cover={s.missing ? null : coverFor(s.doc_id)} className="file-tile">{many ? <i>{String(index + 1)}</i> : null}</CoverChip>
      <div className="source-text">
        <div className="title">{plain(s.title)}</div>
        <div className="sub" title={s.keyword_only ? "Indexed with the other embedding model. Search matches keywords until it's updated." : undefined}>
          {s.missing ? "Missing — add it again" : `${plural(s.pages || 0, "page")}${s.keyword_only ? " · updating search" : ""}`}{active ? <span className="badge">Teaching</span> : null}</div>
      </div>
      <button className="icon-btn xs remove" aria-label={`Remove ${plain(s.title)}`} data-tip="Remove"
        onClick={(e) => { e.stopPropagation(); ctl.removeSource(s.doc_id, s.title); }}>
        <Icon name="trash" className="sm" />
      </button>
    </div>
  );
}

function OutlineBox({ ctl }: { ctl: Ctl }) {
  const o = useStore(ctl.store, (s) => s.outline);
  const error = useStore(ctl.store, (s) => s.outlineError);
  const hasActive = useStore(ctl.store, (s) => !!s.nb.active_source);
  if (!hasActive) return null;
  let content = null;
  if (error) content = <div className="viewer-empty">{error}</div>;
  else if (!o) content = <div className="viewer-empty">Loading the outline…</div>;
  else {
    const current = o.position?.started ? o.position.section_id : null;
    content = (
      <>
        <div className="outline-head">
          <h3>Outline</h3>
          <button className="icon-btn xs" aria-label="Outline options" onClick={(e) => ctl.outlineMenu(e.currentTarget)}><Icon name="more" className="sm" /></button>
        </div>
        <div className="outline">
          {o.sections.length ? o.sections.map((sec) => (
            <button key={sec.id} className={cls("outline-item", `l${Math.min(sec.level, 3)}`, sec.id === current && "current")}
              title={`Teach “${plain(sec.title)}”`} aria-current={sec.id === current ? "location" : undefined}
              onClick={() => ctl.teachSection(sec)}>
              <span className="node" aria-hidden="true" />
              <span className="t">{plain(sec.title)}</span>
              <span className="p">{sec.pages}</span>
            </button>
          )) : <div className="viewer-empty">No sections found.</div>}
        </div>
      </>
    );
  }
  return <div className="outline-wrap">{content}</div>;
}

// ================================================================ conversation

const ChatPanel = memo(function ChatPanel({ ctl }: { ctl: Ctl }) {
  const status = useStore(statusStore, (s) => s.status);
  const keyMissing = !!status && !status.gemini_key;
  return (
    <section className="panel chat" aria-label="Conversation">
      <div className="panel-head">
        <h2>Conversation</h2>
        <button className="icon-btn sm" aria-label="Clear conversation" data-tip="Clear conversation" onClick={ctl.clearChat}><Icon name="trash" className="sm" /></button>
      </div>
      <div className="banner-slot">
        {keyMissing ? (
          <div className="banner"><Icon name="key" />
            <div><b>Voice needs a Gemini API key. </b><KeyHelp /></div>
          </div>
        ) : null}
      </div>
      <div className="panel-body chat-body">
        <ChatMain ctl={ctl} />
        <Dock ctl={ctl} />
      </div>
    </section>
  );
});

function Messages({ ctl }: { ctl: Ctl }) {
  const messages = useStore(ctl.store, (s) => s.messages);
  const sources = useStore(ctl.store, (s) => s.nb.sources);
  const hydrated = useHydrated();
  useLayoutEffect(() => { ctl.flushScroll(); }, [ctl, messages]);
  // Day separators and times appear once local time is known; keep the view pinned to the latest message.
  useLayoutEffect(() => {
    if (!hydrated) return;
    const el = document.querySelector<HTMLElement>(".messages");
    if (el) el.scrollTop = el.scrollHeight;
  }, [hydrated]);

  const items: React.ReactNode[] = [];
  let lastDay = "";
  for (const m of messages) {
    if (hydrated && m.ts) {
      const day = new Date(m.ts).toDateString();
      if (day !== lastDay) {
        lastDay = day;
        items.push(<div key={`d${m.id}`} className="day-sep"><span>{dayLabel(m.ts)}</span></div>);
      }
    }
    items.push(m.role === "user"
      ? <UserMsg key={m.id} m={m} hydrated={hydrated} />
      : <ModelMsg key={m.id} m={m} hydrated={hydrated} sources={sources} ctl={ctl} />);
  }

  return (
    <div className="messages scroll" ref={ctl.ref("messages")}>
      <div className="messages-inner">
        {messages.length ? items : <EmptyState ctl={ctl} />}
      </div>
    </div>
  );
}

const UserMsg = memo(function UserMsg({ m, hydrated }: { m: Msg; hydrated: boolean }) {
  return (
    <div className={`msg user${m.live ? " live" : ""}`} title={hydrated && m.ts ? clockTime(m.ts) : undefined}>
      <div className="bubble">{m.text}</div>
    </div>
  );
});

const ModelMsg = memo(function ModelMsg({ m, hydrated, sources, ctl }: { m: Msg; hydrated: boolean; sources: Source[]; ctl: Ctl }) {
  return (
    <div className={`msg model${m.live ? " live" : ""}`}>
      <div className="avatar" aria-hidden="true"><Mark /></div>
      <div className="content">
        <div className="msg-meta">
          <b>Tutor</b>
          {m.ts && hydrated ? <span>{clockTime(m.ts)}</span> : null}
          <span className="msg-actions">
            <button className="icon-btn xs" aria-label="Copy reply" data-tip="Copy" onClick={() => copyText(m.text)}><Icon name="copy" className="sm" /></button>
          </span>
        </div>
        <div className="activity">{m.acts.map((a) => <ActRow key={a.key} act={a} sources={sources} ctl={ctl} />)}</div>
        <div className={`text${m.interrupted ? " interrupted" : ""}`}>{m.text}</div>
      </div>
    </div>
  );
});

function ActRow({ act, sources, ctl }: { act: Act; sources: Source[]; ctl: Ctl }) {
  return (
    <div className={cls("act", act.running && "running", act.error && "error")}>
      <span className="act-label"><Icon name={act.icon} />{act.label}{act.error ? ` — ${act.error}` : null}</span>
      {act.ui ? <RefChips ui={act.ui} sources={sources} ctl={ctl} /> : null}
    </div>
  );
}

function RefChips({ ui, sources, ctl }: { ui: ToolUI; sources: Source[]; ctl: Ctl }) {
  const title = (docId: string) => plain(sources.find((s) => s.doc_id === docId)?.title) || "Source";
  const chip = (key: string, docId: string, page: number, label: string, snippet: string, tip: string) => {
    const n = sources.findIndex((s) => s.doc_id === docId) + 1;
    return (
      <button key={key} className="ref doc" title={tip} onClick={() => ctl.openCitation(docId, page, snippet)}>
        {sources.length > 1 && n > 0 ? <span className="n">{`${n}`}</span> : <Icon name="file" className="sm" />}
        <span>{label}</span>
      </button>
    );
  };
  if (ui.kind === "search") {
    const seen = new Set<string>();
    return (
      <>
        {ui.refs.filter((r) => { const k = `${r.doc_id}:${r.page}`; if (seen.has(k)) return false; seen.add(k); return true; })
          .slice(0, 5).map((r) => chip(`${r.doc_id}:${r.page}`, r.doc_id, r.page, `p. ${r.pages}`, r.snippet, plain(`${r.title} · ${r.section || ""}`)))}
      </>
    );
  }
  if (ui.kind === "passage") return chip("p", ui.doc_id, ui.page, `${plain(ui.section)} · p. ${ui.pages}`, ui.snippet, plain(title(ui.doc_id)));
  if (ui.kind === "web") {
    return (
      <>
        {ui.links.slice(0, 5).map((l) => (
          <a key={l.url} className="ref web" href={l.url} target="_blank" rel="noopener noreferrer" title={plain(l.title)}>
            <Icon name="globe" className="sm" /><span>{plain(l.site || l.title)}</span>
          </a>
        ))}
      </>
    );
  }
  return null;
}

function EmptyState({ ctl }: { ctl: Ctl }) {
  const sources = useStore(ctl.store, (s) => s.nb.sources);
  const activeId = useStore(ctl.store, (s) => s.nb.active_source);
  const hasSources = sources.length > 0;
  const started = !!sources.find((s) => s.doc_id === activeId)?.position?.started; // like the Dock's quick actions
  const ideas: [string, string][] = hasSources
    ? [["play", started ? "Continue where we left off" : "Teach me from the beginning"],
       ["info", "What is this about?"], ["sparkles", "Quiz me on the first chapter"], ["globe", "What's the latest on this topic?"]]
    : [];
  return (
    <div className="chat-empty" data-empty="">
      <div className="empty-mark"><Mark /></div>
      <h2>{hasSources ? "Ready when you are" : "Add a source to begin"}</h2>
      <p>{hasSources
        ? "Tap the orb and talk, or type below. I'll teach, explain, research and brainstorm, and always tell you what comes from your sources and what comes from the web."
        : "Drop a PDF anywhere on this page, or use Add. It's indexed on your computer, then we can talk about it."}</p>
      {hasSources ? (
        <div className="suggestions">
          {ideas.map(([ic, t]) => <button key={t} className="suggestion" onClick={() => ctl.sendText(t)}><Icon name={ic} className="sm" />{t}</button>)}
        </div>
      ) : <button className="btn primary lg" onClick={ctl.pickFiles}><Icon name="upload" />Add a PDF</button>}
    </div>
  );
}

function ChatMain({ ctl }: { ctl: Ctl }) {
  const floating = useStore(ctl.store, (s) => s.stage.liveOrb);
  return (
    <div className={cls("chat-main", floating && "live-transcript")}>
      <Messages ctl={ctl} />
      <LiveOrb ctl={ctl} />
      <VoiceStage ctl={ctl} />
    </div>
  );
}

// Transcript mode: the orb floats above the text bar, showing what the tutor is doing. Tap it for the
// focused orb view.
function LiveOrb({ ctl }: { ctl: Ctl }) {
  const show = useStore(ctl.store, (s) => s.stage.liveOrb);
  const voice = useStore(ctl.store, (s) => s.voice);
  return (
    <div className="live-orb" hidden={!show} aria-live="polite">
      <button className={`orb-btn live-orb-btn state-${voice.key}`} aria-label="Switch to the orb view" data-tip="Orb view"
        ref={ctl.ref("liveOrbBtn")} onClick={() => ctl.setView("orb")} />
      <div className="live-orb-label"><b>{voice.label}</b><span>{voice.sub}</span></div>
    </div>
  );
}

function VoiceStage({ ctl }: { ctl: Ctl }) {
  const st = useStore(ctl.store, (s) => s.stage);
  const voice = useStore(ctl.store, (s) => s.voice);
  return (
    <div className={cls("voice-stage", st.show && "show", st.compact && "compact", st.tiny && "tiny")} aria-live="polite"
      data-state={voice.key} ref={ctl.ref("voiceStage")}>
      <div className="stage-aura" aria-hidden="true" />
      <div className="stage-top"><button className="btn sm ghost-glass" onClick={() => ctl.setView("transcript")}><Icon name="chat" className="sm" />Show transcript</button></div>
      <div className="stage-center">
        <div className="stage-orb" role="button" tabIndex={0} aria-label="Switch to the transcript view" title="Tap to show the transcript"
          ref={ctl.ref("stageOrb")} onClick={() => ctl.setView("transcript")}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); ctl.setView("transcript"); } }} />
        <div className="stage-label">{st.label}</div>
        <div className="stage-activity">{st.activity}</div>
        <div className={`stage-caption${st.captionUser ? " user" : ""}`}>{st.caption}</div>
      </div>
    </div>
  );
}

// Grows with its content up to `max` px, then scrolls.
function useAutosize(value: string, max: number) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  useLayoutEffect(() => {
    const t = ref.current;
    if (!t) return;
    t.style.height = "auto";
    if (!t.value) { t.style.height = ""; t.style.overflowY = "hidden"; return; }
    t.style.height = `${Math.min(t.scrollHeight, max)}px`;
    t.style.overflowY = t.scrollHeight > max ? "auto" : "hidden";
  }, [value, max]);
  return ref;
}

// The text bar. Idle: type, or tap the orb to start talking. While talking: mute, keep typing, End.
function Dock({ ctl }: { ctl: Ctl }) {
  const voice = useStore(ctl.store, (s) => s.voice);
  const nb = useStore(ctl.store, (s) => s.nb);
  const [text, setText] = useState("");
  const area = useAutosize(text, 140);
  const register = ctl.ref("textarea");
  const live = voice.active;
  const typed = !!text.trim();

  const active = nb.sources.find((s) => s.doc_id === nb.active_source);
  const started = !!active?.position?.started;
  const items = !nb.sources.length ? [] : QUICK.filter(([key]) => (started ? key !== "start" : key !== "continue"));

  const send = () => {
    const value = text.trim();
    if (!value) return;
    setText("");
    ctl.sendText(value);
  };

  return (
    <div className="dock">
      <div className="dock-inner">
        <div className="quick" hidden={!items.length || live}>
          {items.map(([key, ic, label, prompt]) => <button key={key} className="suggestion" onClick={() => ctl.sendText(prompt)}><Icon name={ic} className="sm" />{label}</button>)}
        </div>
        <div className={cls("dock-card", live && "live")}>
          {live ? (
            <button className={cls("mic-btn", !voice.micOn && "off")} aria-label={voice.micOn ? "Mute microphone (M)" : "Unmute microphone (M)"}
              data-tip={voice.micOn ? "Mute (M)" : "Unmute (M)"} onClick={ctl.toggleMic}>
              <Icon name={voice.micOn ? "mic" : "micOff"} />
            </button>
          ) : null}
          <div className="composer">
            <textarea rows={1} placeholder={live ? "Type while you talk…" : "Ask anything…"} aria-label="Message" value={text}
              ref={(el) => { area.current = el; register(el); }}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }} />
          </div>
          {typed ? <button className="send-btn" aria-label="Send message" data-tip="Send" onClick={send}><Icon name="send" /></button> : null}
          {/* Always mounted (only hidden): it holds the voice orb's WebGL canvas. */}
          <button className={`orb-btn bar-orb state-${voice.key}`} aria-label="Start a voice conversation" data-tip="Talk"
            hidden={typed || live} onClick={ctl.onOrb} ref={ctl.ref("orbBtn")}>
            <span className="orb-icon"><Icon name="mic" className="lg" /></span>
          </button>
          {live ? (
            <button className="end-btn" aria-label="End conversation (Esc)" data-tip="End (Esc)" onClick={ctl.endLive}>
              <Icon name="hangup" className="sm" />End
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

// ================================================================ viewer

const ViewerPanel = memo(function ViewerPanel({ ctl }: { ctl: Ctl }) {
  const v = useStore(ctl.store, (s) => s.viewer);
  const reading = useStore(ctl.store, (s) => s.reading);
  const [pageText, setPageText] = useState("");
  const editing = useRef(false);
  useEffect(() => { if (!editing.current) setPageText(v.docId && v.page ? String(v.page) : ""); }, [v.docId, v.page]);

  const openHref = v.docId && v.page ? `${api.fileUrl(v.docId)}#page=${v.page}` : undefined;
  return (
    <section className="panel viewer" aria-label="Page viewer" ref={ctl.ref("viewerPanel")}
      onPointerMove={ctl.viewerPointer} onPointerDown={ctl.viewerPointer}>
      <div className="panel-head viewer-head">
        <span className="doc-ic"><Icon name="file" className="sm" /></span>
        <div className="doc-name" title={plain(v.title) || undefined}>{plain(v.title) || "Page viewer"}</div>
        <div className="zoom">
          <button className="icon-btn sm" aria-label="Zoom out" onClick={ctl.zoomOut}><Icon name="zoomOut" className="sm" /></button>
          <button className="zoom-label" aria-label="Fit to width" data-tip="Fit width" onClick={ctl.fitWidth}>{`${Math.round(v.zoom * 100)}%`}</button>
          <button className="icon-btn sm" aria-label="Zoom in" onClick={ctl.zoomIn}><Icon name="zoomIn" className="sm" /></button>
        </div>
        {openHref ? (
          <a className="icon-btn sm" aria-label="Open the PDF in a new tab" data-tip="Open PDF" target="_blank" rel="noopener" href={openHref}><Icon name="external" className="sm" /></a>
        ) : (
          <button className="icon-btn sm" aria-label="Open the PDF in a new tab" disabled><Icon name="external" className="sm" /></button>
        )}
        <button className="icon-btn sm" aria-label={reading ? "Exit full screen (F)" : "Full-screen reading (F)"} data-tip={reading ? "Exit full screen (F)" : "Full screen (F)"} onClick={ctl.toggleReading}>
          <Icon name={reading ? "minimize" : "maximize"} className="sm" />
        </button>
        <button className="icon-btn sm viewer-close" aria-label="Close viewer" onClick={ctl.toggleViewer}><Icon name="x" className="sm" /></button>
      </div>
      {/* The PDF viewer engine owns this element's children. */}
      <div className="viewer-stage scroll" tabIndex={-1} ref={ctl.ref("stage")} />
      <div className="viewer-foot">
        <button className="icon-btn sm" aria-label="Previous page" onClick={ctl.prevPage}><Icon name="left" className="sm" /></button>
        <span className="page-nav">
          <input className="page-input" type="text" inputMode="numeric" aria-label="Page number" value={pageText}
            onChange={(e) => setPageText(e.target.value)}
            onFocus={(e) => { editing.current = true; e.currentTarget.select(); }}
            onBlur={() => { editing.current = false; setPageText(v.docId && v.page ? String(v.page) : ""); }}
            onKeyDown={(e) => {
              if (e.key === "Enter") { const n = parseInt(pageText, 10); if (n) ctl.goToPage(n); e.currentTarget.blur(); }
              if (e.key === "Escape") { e.currentTarget.blur(); }
            }} />
          <span className="page-total">{v.docId && v.total ? `/ ${v.total}` : ""}</span>
        </span>
        <button className="icon-btn sm" aria-label="Next page" onClick={ctl.nextPage}><Icon name="right" className="sm" /></button>
      </div>
    </section>
  );
});

// ================================================================ reading mode: floating tutor

function FloatDock({ ctl }: { ctl: Ctl }) {
  const b = useStore(ctl.store, (s) => s.bubble);
  const voice = useStore(ctl.store, (s) => s.voice);
  const open = useStore(ctl.store, (s) => s.composerOpen);
  const [text, setText] = useState("");
  const area = useAutosize(text, 110);
  const register = ctl.ref("floatInput");
  const send = () => { const value = text.trim(); if (!value) return; setText(""); ctl.sendFloat(value); };
  const who = b.who === "user" ? "You" : "Tutor";

  return (
    <div className="float-dock" ref={ctl.ref("floatDock")} onMouseEnter={() => ctl.dockHover(true)} onMouseLeave={() => ctl.dockHover(false)}>
      <div className={cls("float-bubble", b.show && "show", b.who === "user" && "user", b.busy && "busy")} aria-live="polite">
        <div className="bubble-head">{b.busy ? <span className="bubble-act"><Icon name={b.actIcon} className="sm" />{b.actLabel}</span> : <b>{who}</b>}</div>
        <div className="bubble-text">{b.text}</div>
      </div>
      <div className="float-row">
        <div className="float-composer" hidden={!open}>
          <textarea rows={1} placeholder="Ask about this page…" aria-label="Message" value={text}
            ref={(el) => { area.current = el; register(el); }}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); }
              if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); ctl.toggleComposer(false); }
            }} />
          <button className="send-btn sm" disabled={!text.trim()} aria-label="Send message" onClick={send}><Icon name="send" className="sm" /></button>
        </div>
        <div className="float-actions">
          <button className={cls("float-btn", open && "on")} aria-label="Type a message (/)" data-tip="Type (/)" onClick={() => ctl.toggleComposer()}><Icon name="keyboard" /></button>
          <button className="float-btn end" aria-label="End conversation" data-tip="End" hidden={!voice.active} onClick={ctl.endLive}><Icon name="hangup" /></button>
        </div>
        <button className={`orb-btn float-orb state-${voice.key}`} aria-label={voice.aria} onClick={ctl.onOrb} ref={ctl.ref("floatOrbBtn")}>
          <span className="orb-icon"><Icon name={voice.icon} className="lg" /></span>
        </button>
      </div>
    </div>
  );
}

// ================================================================ phone tabs, drop overlay

function MobileTabs({ ctl }: { ctl: Ctl }) {
  const tab = useStore(ctl.store, (s) => s.layout.tab);
  const tabs: [Tab, string, string][] = [["sources", "list", "Sources"], ["chat", "chat", "Chat"], ["page", "file", "Page"]];
  return (
    <nav className="mobile-tabs" aria-label="Sections">
      {tabs.map(([key, ic, label]) => (
        <button key={key} className={key === tab ? "on" : ""} data-tab={key} aria-current={key === tab ? "page" : undefined}
          onClick={() => ctl.setTab(key)}><Icon name={ic} />{label}</button>
      ))}
    </nav>
  );
}

function DropOverlay({ ctl }: { ctl: Ctl }) {
  const drop = useStore(ctl.store, (s) => s.drop);
  const name = useStore(ctl.store, (s) => s.nb.name);
  return (
    <div className={cls("drop-overlay", drop && "show")}>
      <div className="drop-card">
        <span className="drop-ic"><Icon name="upload" className="lg" /></span>
        <b>Drop PDFs to add them</b>
        <span>{`to “${name}”`}</span>
      </div>
    </div>
  );
}
