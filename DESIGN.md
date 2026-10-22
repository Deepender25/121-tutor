# Voice Tutor — Design

> Source of truth for what we're building and why. Supersedes `docs/HANDOFF.md` where they differ.
> External facts verified 2026-10-10 (§10). Anything marked *test* still needs confirming.

## 1. Product

A **local web app** (NotebookLM-style) where each **notebook** holds one or more PDFs, and you have a
**1:1 real-time voice conversation** about them. The same conversation switches roles by what you say:

| You say… | Role | What happens |
|---|---|---|
| "Start from the beginning" / "Next" / "Go on" | **Tutor** | Walks through the active source in order, one small part at a time, explains in its own words, checks understanding |
| "Skip to chapter 3" / "Go to page 40" / "Switch to my other PDF" | **Navigator** | Moves the reading position (or active source) and continues from there |
| "What does it say about X?" | **Explainer** | Searches all sources, answers with page references |
| "Research this" / "Is this still true?" | **Researcher** | Web search, clearly labelled as web vs your sources |
| "Help me think of…" | **Brainstormer** | Builds on the material, says when it goes beyond it |
| "Quiz me" | **Examiner** | One question at a time from the sources |
| "Where were we?" | **Guide** | Position, outline, what's covered (superseded: see §27 and §31) |

**Non-negotiable behaviours**
1. **Sources first**: topic questions start with `search_sources`; pages spoken naturally.
2. **Web second, labelled**: "Your document says… / A current web source, <site>, says…".
3. **Honest gaps**: "Your sources don't cover that — want me to look it up?"
4. **Remembers**: per-PDF reading position + covered sections, per-notebook conversation history. (superseded: see §27 and §31)
5. **Interruptible**: talk over it any time (server VAD + instant client playback flush).

## 2. Architecture

```
Browser ──► UI server (frontend/, Next.js :8765) ── /api/* and the voice WebSocket proxied ──► API (:8766)

Browser (React UI)                     API server (tutor/server, FastAPI)                   Google
┌──────────────────────────┐  WS   ┌────────────────────────────────────────┐  WS   ┌──────────────────┐
│ mic → worklet → 16k PCM ─┼──────►│ live.py bridge ── GeminiLiveSession ───┼──────►│ gemini-3.8-live  │
│ speaker ◄─ 24k PCM ◄─────┼◄──────┤   tools run locally (BLOCKING):        │◄──────┤ STT·LLM·TTS·VAD  │
│ transcript, tool chips,  │ JSON  │   get_outline · read_passage ·         │       └──────────────────┘
│ FluidOrb, page viewer    │       │   search_sources · web_search ─────────┼──► DuckDuckGo / Tavily
└──────────▲───────────────┘       │ REST: notebooks, uploads(jobs), pages  │
           │ REST                  │ library/lesson/notebooks (JSON)        │
           └───────────────────────┤ LanceDB per PDF (vectors + FTS)        │
                                   │ EmbeddingGemma 2 (CPU)                 │
                                   └────────────────────────────────────────┘
Upload: PDF ─► parse (pymupdf4llm + get_toc) ─► sections ─► ordered chunks ─► embed ─► LanceDB
```

- The API key stays on the server; the browser never talks to Google directly.
- **Ordered chunks** serve both teaching (cursor walks `order`) and answering (hybrid search).
- **Position & progress live in our code** (`progress.json` per PDF) and are injected into the system
  prompt on every (re)connect, because context compression drops old turns.
- The voice layer is behind `tutor/voice/base.py` so a Pipecat/Groq/Kokoro cascade can replace Gemini.

## 3. Ingestion

1. `pymupdf4llm.to_markdown(page_chunks=True)` → per-page markdown + layout boxes. Boxes classed
   `page-header`/`page-footer`/`picture` are dropped (running headers gone). Empty boxes (a pymupdf4llm
   quirk) fall back to raw text from the bbox.
2. Structure: PDF outline (`get_toc`) → else detected headings (level from numbering "2.2" when present)
   → else synthetic "Pages 1–5" sections.
3. Chunk within sections: ~550 tokens target, 750 max, ~70 overlap (overlap skipped when teaching).
4. Embed with the document prompt `title: <heading path> | text: …`.
5. LanceDB table + native FTS index over `heading_path + text` (`search_text`).
6. `meta.json` records model/dim/prompt/chunker/index versions; a mismatch re-indexes automatically.
   **Never mix embedding models in one table.**

Same file uploaded twice (same SHA-256) reuses the index, even across notebooks.

## 4. Data (all under `%LOCALAPPDATA%\voice-tutor`, outside OneDrive)

```
files/<doc_id>.pdf                 uploaded PDFs
docs/<doc_id>/lancedb/             chunks table (vector 768d + FTS)
docs/<doc_id>/sections.json        ordered sections with chunk-order ranges
docs/<doc_id>/meta.json            title, pages, versions
docs/<doc_id>/progress.json        cursor, covered chunk orders, last summary (superseded: see §27 and §31)
docs/<doc_id>/pages/<n>@<s>.png    rendered page cache
notebooks/<id>.json                name, emoji, sources[], active_source, history[]
settings.json                      voice
```
A source's index is deleted only when no notebook uses it any more.

## 5. Tools (tutor/tools.py)

| Tool | Purpose |
|---|---|
| `get_outline(source?)` | Sources list + outline/progress of the active or chosen source |
| `read_passage(target, source?)` | Teach in order; targets: next/current/previous/start, `s012`, `page:40`, "chapter 3", "2.1", title words. Choosing another source makes it active |
| `search_sources(query, source?, top_k)` | Hybrid search across all (or one) sources |
| `web_search(query)` | DuckDuckGo (`ddgs`), or Tavily when `TAVILY_API_KEY` is set (falls back to DDG) |

Sources are referenced by number ("2") or title words. Each call returns `(result for the model, UI
event)`; UI events drive citation chips, the page viewer and progress refreshes.

**Search across sources**: per-table hybrid scores are rank-based, so merging them would make every
PDF's best chunk look equally relevant. We pool vector hits (cosine distances are comparable) and BM25
hits globally and fuse once with RRF (k=60) — `tutor/retrieval/search.py`.

## 6. Conversation behaviour

`prompts/system.md`, filled per connection by `tutor/voice/prompt.py` with: notebook name, numbered
sources (active one with a 2-level outline incl. section ids and covered marks), reading position, and
the last ~10 turns of history. Key rules: sources first, web labelled with site name, teaching loop
(read → explain simply → check → wait), short spoken turns, no markdown/URLs, reply in the user's
language (Hindi/English mixing mirrored), never talk over the user (3.8 proactive audio is always on). (superseded: see §27 and §31)

## 7. Voice session (tutor/voice/gemini_live.py)

- Audio in 16 kHz PCM16 (browser worklet downsamples), out 24 kHz PCM16; input+output transcription on.
- Tools declared `behavior: BLOCKING` (3.8 defaults to non-blocking, which could answer before the
  passage arrives). Tool calls run in tasks so cancellations/interruptions are still processed.
- Context-window compression on (no session time cap). Resumption handles saved; on GoAway or a
  dropped connection it reconnects with the handle (up to 4 retries with backoff).
- Fatal errors (quota, bad key, unknown model) are reported in plain language; no retry loop.
- Idle sessions close after 5 minutes to save free-tier quota.
- Turn transcripts + citations are saved to notebook history on `turn_complete` (and on barge-in).

## 8. Web app (frontend/: Next.js App Router, React, TypeScript — see §20)

- `run.bat` → "Voice Tutor - API" window (`app.py` → uvicorn :8766) and "Voice Tutor - UI" window
  (`ui.py` → `next start` :8765). Routes: `/` (notebooks), `/notebooks/[id]` (one notebook).
- **Home**: notebook grid (create / rename / delete).
- **Notebook**: Sources panel (upload with progress, active source, outline with progress ticks — click
  a section to have it taught) · Conversation (transcript, tool activity chips, citation chips, quick
  actions, composer, voice dock) · PDF viewer (continuous vertical scroll; highlights cited passages,
  opens PDF; moves only when you ask, §30). Draggable splits between the panels; full-screen reading mode (§18).
- **Voice mode**: WebGL **FluidOrb** (shader from the user-provided component, unchanged) in the dock and
  as a large orb on a focused stage with live captions; flow speed and scale follow the audio level.
- Light/dark/system themes; responsive down to phone width (tabbed panels); container queries for the
  chat column; reduced-motion respected.

## 9. Layout

```
app.py              API server (run.bat: "Voice Tutor - API") run.bat   venv, deps, .env, UI build, 2 windows
ui.py               UI build/serve (run.bat: "Voice Tutor - UI")
console.py          text console (debug)                     ingest.py · query.py   CLIs
prompts/system.md   conversation behaviour
tutor/
  config.py document.py lesson.py library.py logs.py notebooks.py tools.py web.py
  ingest/  parse.py chunk.py
  retrieval/  embedder.py store.py search.py
  voice/  base.py gemini_live.py prompt.py
  server/  app.py live.py jobs.py pages.py state.py
frontend/
  next.config.ts   /api rewrite to the API (incl. WebSocket), proxy limits
  public/          favicon.svg, brand/mark.svg, audio/recorder-worklet.js
  src/app/         layout.tsx (fonts, theme script, globals.css), page.tsx, notebooks/[id]/page.tsx,
                   not-found.tsx, error.tsx, ui-health/route.ts
  src/components/  Icon, overlays (toasts/dialogs/menus), Settings, AppShell, RouteStates,
                   home/HomeView, notebook/{controller.ts, NotebookView.tsx}
  src/lib/         api (browser), server-api (server rendering), live, pdf-view, fluid-orb,
                   store, status, theme, format, icons, types
tests/  unit + API integration tests, fixtures/make_sample_pdf.py, questions_sample.yaml
```

## 10. Verified facts (2026-10-10)

| Fact | Status |
|---|---|
| `gemini-3.8-live` stable, free on free tier | confirmed (pricing page + live sessions work with our key) |
| 3.1-flash-live-preview & 2.5 native-audio preview shut down 2026-11-17 | confirmed — not used |
| Function calling (BLOCKING) + transcription + resumption + compression on 3.8 Live | **confirmed live with our key** |
| Built-in `google_search` on Live (and search grounding on Flash) | **zero free-tier quota on this key** → own `web_search` tool |
| Audio in 16 kHz / out 24 kHz PCM16 | confirmed |
| 15 min audio cap without compression; ~10 min connection; handle valid 2 h | confirmed (docs) |
| Context window 131,072 in | confirmed (docs) |
| Free-tier Live concurrency | UNCONFIRMED — see AI Studio |
| Hindi supported; Hinglish code-switching | Hindi confirmed (docs); Hinglish *test* |
| EmbeddingGemma 2: ungated, Apache 2.0, 8K ctx, 768d, no fp16; needs ST ≥ 6.1, transformers ≥ 5.18, **torchvision + Pillow** | confirmed |
| LanceDB 0.40: native FTS (`create_index(col, config=FTS())`), hybrid limit applies before fusion | confirmed / observed |
| pymupdf4llm 1.28 box classes (`page-header`, `section-header`…) | confirmed |

## 11. Decisions log

| Decision | Why |
|---|---|
| Gemini Live 3.8 for the voice loop | Free; one connection does STT+LLM+TTS+turns |
| Server-side relay (not browser→Google with ephemeral tokens) | Key stays local; tools run next to the index; one place for session logic |
| Own `web_search` (DDG / Tavily) instead of built-in search | Built-in has no free quota on this key; also gives explicit sources for attribution and UI links |
| Retrieval instead of whole-PDF context | Audio eats the 131K window; exact page cites; teaching needs ordered passages |
| EmbeddingGemma 2 over v1 | Ungated, 8K ctx, ≥ v1 quality |
| LanceDB, one table per PDF + global RRF across a notebook | Simple re-indexing per file; correct cross-source ranking |
| Vanilla JS frontend, no build | `run.bat` needs nothing but Python; fewer moving parts |
| Data outside OneDrive; venv in `.venv` inside the project | Sync locks Lance files. The venv lives in the project because the user wants it visible and self-contained (cost: OneDrive syncs ~2 GB; slower first install) |
| Server in its own console window, logs to `logs/` | Visible live activity; files for later debugging. Console logging goes through a queue and QuickEdit is disabled, because a paused Windows console otherwise blocks the whole server (observed) |
| State in our code, injected on connect | Compression forgets; resumption expires |

## 12. Status (2026-10-10)

Done and verified:
- 28 tests pass (unit: chunker, lesson cursor, parser; API integration: upload→index→outline→page
  render→highlight→search→shared-source lifecycle→live socket errors).
- Retrieval eval on the sample: 14/15 at top-1, 15/15 at top-3.
- Live in the browser UI (typed input): source-grounded answer with page cite, teaching from the start
  with viewer follow + highlight, citation chips, history persistence, light/dark, phone layout.
- Live **spoken** input end-to-end through the server: speech transcribed exactly, `search_sources`
  called, correct cited answer spoken; first audio ≈ 2.3 s after speech ended (incl. VAD wait + search).

Next:
- Run the eval on a real PDF the user studies; tune chunking if needed.
- Real-mic session in Chrome/Edge on the user's machine (the in-app browser blocks the mic).
- Hinglish test; long-session test (30+ min, resumption).

## 13. Privacy

Parsing, indexing and search are local. Speech and retrieved passages go to Gemini; free-tier content
may be used by Google to improve products. Don't describe the app as private.

## 14. Launcher & logs

- `run.bat`: (1) create `.venv` with the py launcher (3.13 → 3.10, skipping conda interpreters) and
  verify `sys.prefix != sys.base_prefix`; (2) install `requirements.lock` (+ PyTorch CPU index) when the
  lock changed or `find_spec` can't locate a core package, then verify by importing; pip log →
  `logs/setup.log`; (3) create `.env`, warn on empty key; (4) `app.py --open-if-running`, else
  `start "Voice Tutor - Server" cmd /k call .venv\Scripts\python.exe app.py`. (superseded: see §27 and §31)
- `tutor/logs.py`: console (coloured, INFO, via QueueListener) + `logs/voice-tutor.log` (DEBUG, 5 MB × 5)
  + `logs/errors.log` (WARNING+). Request middleware logs mutations at INFO, polling GETs at DEBUG.
  Voice sessions log start/end, Gemini connect time, tool calls with args/result/ms, reply latency and
  each exchange (truncated). Uvicorn runs with `log_config=None` so its logs use the same handlers.


## 15. UI system & robustness notes (2026-10-10)

- Design tokens in `web/css/app.css` (light/dark share names); accent gradient from the voice orb;
  glass top bar; settings as a side sheet with explicit **Save**; home has a "Continue" card and
  notebook covers coloured by a hash of the id; sources show progress rings; outline has a progress rail;
  voice mode is a full stage (orb sized to the available height, compact layout under 380 px).
- **Settings deadlock (fixed):** `update_settings` held a non-reentrant lock and called `settings()`,
  which took it again → every `/api/status` hung. Now a lock-free `_read_settings()` + `RLock`, atomic
  write, and a regression test with a timeout.
- **Stale browser caches (fixed):** `/` is rendered by the server with an import map that stamps every
  module `?v=<newest web file mtime>`; all static files are `Cache-Control: no-cache`. A browser can no
  longer mix old and new modules after an update.
- Every API call has a client-side timeout (status 8 s, default 20 s, uploads 10 min) so the UI never
  freezes on a stuck server.
- Source titles derived from file names are prettified (`Unit2_Notes` → `Unit2 Notes`) when read, so
  the voice model doesn't read underscores aloud.

## 16. Conversation quality & resilience fixes (2026-10-10, from a real 12-minute session)

- **Reconnect bug:** after two Gemini `1011 Internal error`s, resuming failed with `1008 session not found
  or expired`; the old check matched "not found", called it fatal ("model not available") and ended the
  session. Now `classify_error()` → FATAL (quota/key/model) | STALE_HANDLE (drop handle, fresh session
  with recent history carried in the prompt) | TRANSIENT (backoff; after 2 failed resumes, go fresh).
  The UI gets a "Voice reconnected" notice. Recent history in the prompt raised to 16 turns / 6,000 chars.
- **Too-short explanations:** caused by the old prompt ("under 20 seconds", "a few sentences"). New prompt:
  cover every point/list item/date, no summarizing unless asked, read a whole section (read_passage next
  until is_section_end) and use top_k 8 for broad topics; follow the person's lead instead of pushing
  "ready for the next part?". Verified: "history of AI" went from ~50 words (1950–56 only) to ~200 words
  covering 1950 → ChatGPT in order.
- **Pretend web searches:** prompt now forbids claiming a search without calling web_search, and the live
  bridge has a guard (`tutor/intent.py` + `WebSearchGuard`): if the person asked for an internet search
  (English/Hindi/Hinglish) and the turn ended without web_search, the server tells the model to search
  now. Observed firing in a live test, followed by a real search.
- **Language drift:** misheard words had switched replies to Spanish/Thai; prompt restricts to English or
  Hindi (Hinglish mirrored).
- **Cut-offs mid-thought:** VAD end-of-speech sensitivity LOW + 1,000 ms silence (`VOICE_TUTOR_VAD_SILENCE_MS`).
- Transcript pieces after a tool call are joined with the missing space ("you.The" → "you. The").

## 17. Brand & design language (2026-10-10)

- **Logo:** the four-blade swirl, traced from the supplied artwork into `web/brand/mark.svg` /
  `web/js/mark-path.js` (single path, `currentColor`). Used for the top-bar brand, the home hero, the
  chat avatar and `favicon.svg` (dark on light, light on dark). Static everywhere — no spinning.
- **Colour:** ink on paper (`--ink` #141416 / `--bg` #f4f3ef; dark: #f2f2f4 / #0d0d0f). Solid colour only:
  no gradients, no blue/violet. Status colours (green/amber/red) only for status; yellow only for page
  highlights.
- **Voice box:** the FluidOrb shader (user-supplied, unchanged) in monochrome (`--orb-color` charcoal in
  light, pearl in dark). Flow speed and scale follow the audio level (fast attack, slow release), faster
  churn while connecting/thinking, desaturated when the mic is muted. The orb's own shading is the one
  exception to "no gradients", by request.
- **Icons:** Lucide 1.48 (ISC), vendored in `web/js/icons.js` (76 icons). Each gets an `ic-<name>` class
  with a small purposeful hover/press motion in CSS. Notebooks use Lucide icons (24 choices) instead of
  emoji; legacy emoji notebooks are mapped automatically.
- **Fewer buttons:** status pill only when something needs attention; theme lives in Settings; no speaker
  mute; quick actions trimmed to 4; the big "add PDF" tile only for empty notebooks; no duplicate "new
  notebook" card.
- **Voice previews:** `GET /api/voices/{voice}/preview` → Gemini Live speaks a short line in that voice
  once; WAV cached in `DATA_ROOT/voice-previews/`. Settings has a play button per voice.
- Headings from PDFs are stripped of stray HTML tags (e.g. `<mark>`), including in existing indexes.

## 18. PDF viewer, resizable layout, reading mode (2026-10-10)

- **Continuous viewer** (`web/js/pdf-view.js`): every page stacked vertically like a desktop PDF reader.
  `GET /api/docs/{id}/pages` returns all page sizes (cached as `pages/sizes.json`), so the whole document
  is laid out before any image loads and nothing jumps. An IntersectionObserver renders pages within ~1.5
  screens and releases the rest (memory stays flat on long PDFs). Render scale = on-screen width × DPR,
  rounded up to 0.25 steps so the server's PNG cache is reused. 100% = fit width, capped at 920 px.
  Zoom (buttons, Ctrl+wheel, trackpad pinch) and any resize keep the reading position; sharper renders
  swap in without flashing. A citation scrolls to the page and the highlighted passage.
- **Resizable layout**: grid columns `sources | gutter | chat | gutter | viewer`. Drag a gutter, arrow
  keys on a focused gutter, double-click to reset. Sources width in px (220–440; dragging below 150
  collapses it, click the handle to reopen); viewer as a share of the width (scales with the window);
  the conversation keeps ≥ 340 px. Saved per browser (`localStorage["vt-split"]`). The viewer stays in
  the grid down to 981 px wide, an overlay below that, a tab on phones.
- **Reading mode** (⛶ in the viewer, or F): real browser full screen (`documentElement.requestFullscreen`,
  so dialogs/toasts still show; falls back to filling the window if refused). The PDF fills the screen
  and scrolls freely; its toolbars float as glass pills that fade after 2.6 s idle. The tutor becomes a
  floating FluidOrb at the bottom right (tap = talk/mute) with a speech bubble showing the live caption,
  what you said, and tool activity ("Searching the web…"); it fades a few seconds after a turn. A small
  type button opens an inline composer. Esc (or the browser's own exit) leaves reading mode; covered orbs
  pause rendering.

## 19. Follow-along (2026-10-10; removed, §30)

Problems with the first version: the page jumped as soon as a passage was *fetched* (Gemini generates
audio ~4× faster than it plays, so it often jumped while the tutor was still on the previous part); only
the first few words of a passage were highlighted, so it never advanced; answers from `search_sources`
never moved the page; and it yanked you back if you had scrolled away.

How it works now:
- **Material**: `read_passage` and `search_sources` results carry the full chunk text to the browser
  (stripped from saved history by `without_text`).
- **Tracking** (`frontend/src/lib/follow.ts`): rebuilt in §24 (passage first, then sentence).
- **Timing**: every transcript piece and tool result is applied at the moment its audio is heard:
  `LiveClient.playbackLag` (queued, unheard audio) delays it. Interruptions cancel what was never heard.
  Captions and the reading-mode bubble use the same timing, so they show what is being *said*.
- **Locating** (`GET /api/docs/{id}/locate?text=&first=&last=` → `pages.locate_text`): fuzzy in-order
  alignment of the sentence's words against the page's words (natural block order so table cells stay whole,
  NFKC for ligatures, end-of-line hyphenation joined, ±1 page tolerance), returning one rectangle per printed
  line. Page words are cached. On both real PDFs: 1432/1434 sentences located (99.9%), ~2–6 ms cached.
- **Behaviour**: a passage being taught shows its opening when its audio starts; search hits don't move the
  page until the tutor's words show which hit it's using; if a sentence can't be placed the current
  highlight stays. The highlight only scrolls when it isn't comfortably visible.
- **Pause**: scrolling, paging, or clicking a citation pauses following (the highlight keeps tracking in
  place); a "Back to the tutor · p. N" pill jumps back; asking anything new resumes automatically.

## 20. Frontend on Next.js (2026-10-10)

- **Stack:** Next.js 16.3 (App Router, Turbopack), React 19.3, TypeScript 5.9 (strict). Versions pinned
  exactly; `package-lock.json` committed. The Python FastAPI server stays the backend.
- **Two processes:** the UI (`next start`, :8765, the address you open) and the API (uvicorn, :8766).
  `next.config.ts` rewrites `/api/:path*` to the API. Verified in the installed Next source that external
  rewrites also proxy WebSocket upgrades with no idle timeout, so the live voice socket goes through the
  same origin. HTTP proxying defaults to a 30 s timeout and a 10 MB buffered body (larger bodies are cut
  off), so `experimental.proxyTimeout` = 10 min and `proxyClientMaxBodySize` = 1 GB (PDFs up to 200 MB).
- **Routes:** `/` and `/notebooks/[id]` are server-rendered per request (`connection()` + `no-store`) from
  the API (`lib/server-api.ts`, `React.cache` so the page and its metadata share one request). Missing
  notebooks return a real **404** (no root `loading.tsx`, so nothing streams before the check), the API
  being down renders a "server offline" state with Retry (`router.refresh()`), unexpected errors use
  `error.tsx` (`retry`, stable since 16.3). Old `/#/nb/<id>` links redirect to `/notebooks/<id>`.
  Titles: "Voice Tutor" / "<notebook> · Voice Tutor". Back/Forward, new tabs and bookmarks work.
- **Same look:** `globals.css` is the previous stylesheet unchanged except the font names; components
  render the same markup and class names. Fonts are bundled (`@fontsource/inter` 400–700 static,
  `@fontsource-variable/fraunces` with optical sizing), so nothing is fetched from Google and nothing flashes.
  The theme is applied by an inline script before first paint (`suppressHydrationWarning` on `<html>`).
- **Hydration-safe:** values that differ between server and browser (greeting, local times, day
  separators, "2 hours ago") render after hydration (`useHydrated`). No hydration warnings in the console.
- **Notebook page:** `controller.ts` holds all behaviour (voice session, splits, reading mode,
  uploads, keyboard) and exposes a small external store; `NotebookView.tsx` renders from it. Imperative
  engines (PdfView, FluidOrb, LiveClient) are the same code as before, typed. On open, the
  page re-checks its data with the API (pages can come from the browser's back/forward cache).
  No `router.prefetch` on hover: it caches the whole notebook page for 5 minutes (stale history).
- **Launcher:** `ui.py build` checks Node ≥ 20.9, runs `npm ci` when `package-lock.json` changes and
  `next build` when any frontend file or the API address changes (hash stamp); `ui.py serve` runs the
  server with output to its window and `logs/ui.log`, and opens the browser once UI and API answer.
  Next telemetry is disabled. The API server's `/` redirects to the UI.

## 21. Embedding model in its own process (2026-10-10)

Reported: on another computer, uploading a PDF parsed fine, then the whole app closed as soon as embedding
began. Model loading had worked (it happens at start-up), so the crash was the first forward pass: a native
crash inside PyTorch (no Python exception, so the job couldn't fail gracefully; the process just died).
Likely causes on other hardware: a processor without the instructions some maths kernels use (common on
budget Celeron/Pentium/Atom chips), memory running out, or a conflicting OpenMP runtime.

- `tutor/retrieval/worker.py`: the model runs in a spawned worker process; the server talks to it over a
  pipe (`EmbedderClient`, same interface as `Embedder`). The tokenizer stays in the server (chunking calls it
  per paragraph) and loads in parallel with the worker.
- Before importing torch the worker sets `KMP_DUPLICATE_LIB_OK=TRUE` (duplicate OpenMP must not abort), caps
  threads, enables `faulthandler` into `logs/embedder-crash.log`, logs CPU name, cores, RAM, torch version and
  the CPU kernels torch picked (AVX2/AVX-512/DEFAULT), and runs a warm-up with chunk-length inputs.
- If the worker dies, the server explains why from the exit code (0xC000001D illegal instruction -> processor;
  no-memory codes or little free RAM -> memory; 3 -> OpenMP abort; otherwise the code and free RAM), restarts
  it in **safe mode** (`ATEN_CPU_CAPABILITY=default`, `ONEDNN_MAX_CPU_ISA=SSE41`, `MKL_ENABLE_INSTRUCTIONS=SSE4_2`,
  oneDNN off, at most 4 threads) and retries the batch one at a time. If safe mode also crashes, the upload
  fails with that explanation and the app keeps running. Out-of-memory errors retry with smaller batches
  (8 -> 4 -> 2 -> 1).
- Verified by killing the worker mid-indexing: it restarted in safe mode (torch reported DEFAULT kernels),
  finished the document, and the vectors match normal mode. Batch size 16 -> 8 lowers peak memory. Start-up
  31 s (tokenizer loads in parallel).
- The API process also enables `faulthandler` (`logs/crash.log`).

## 22. Text bar and voice views (2026-10-10)

Modelled on ChatGPT's voice: one pill-shaped text bar. Idle: "Ask anything…" with the orb as the voice
button on the right (it swaps for a round send button while there's text). Tapping the orb starts talking;
the bar becomes mic toggle | "Type while you talk…" | **End**. Two views while talking, remembered in
`localStorage["vt-voice-view"]`: **transcript** (default; the conversation stays visible and a 68 px
FluidOrb floats over its bottom with the status, e.g. "Speaking · just talk to interrupt"; the list gets
bottom padding and stays pinned to the latest message when the orb appears) and **orb** (the focused stage
with the big orb and captions). Tapping the floating orb opens the orb view; tapping the big orb or "Show
transcript" returns. The stage's own mic/End buttons were removed (the bar has them). Quick suggestions
hide while talking. The bar's orb pauses rendering while hidden; the floating orb exists only while shown.

## 23. Completion tracking (2026-10-10)

The old logic only counted chunks read with read_passage, so answers given from search_sources (most
teaching) never counted, jumping into a section counted only its first chunk, parents almost never
completed, and nobody could mark anything ("I can't physically mark the content as completed").

- **Model** (`tutor/lesson.py`): per chunk, `read` (read_passage) and `done` (marked). A section's status is
  derived from its whole subtree: `completed` (every chunk finished), `in_progress` (some), `not_started`.
  Parents roll up automatically; the percentage is finished chunks / all chunks (content-weighted).
  Unmarking clears both read and done for the subtree. `progress.json` gains `done`; old files load.
- **Marking** (`Lesson.mark(target, completed, through)`): target = section id, number ("14", "point 14",
  "chapter 3"), title words, page, or "all"; `through` = from the start up to and including the target.
  Completing moves the reading position to the end of what was completed when it reaches it, so "continue"
  resumes at the first part that isn't done.
- **Tutor tool** `update_progress(target, completed=true, through=false, source)`; the prompt's outline shows
  each section's status, and the system prompt says to use the tool whenever asked to mark/skip/unmark and
  after teaching a whole section without read_passage. The UI shows "Marked everything up to and including
  2 System Components · 64% covered" and refreshes the outline and rings immediately; it's kept in history.
- **UI**: the outline circle is a tick button (Shift+click = up to here), the title teaches; in progress is a
  tinted circle; the outline menu has "Mark all as completed" and "Reset progress".
  REST: `POST /api/docs/{id}/progress {target, completed, through}` → outline.
- Verified live: "mark all the content completed till up to point 2" → the tutor called update_progress and
  confirmed; unticking 2.1 made chapter 2 "in progress" (55%); Shift+click on 3.1 completed through 3.1 (82%).

## 24. Follow-along v3: passage first, then sentence (2026-10-10; removed, §30)

Complaint: the page "jumps to different sections". Cause, measured: the tracker matched the last 18
spoken words against every sentence of every candidate at once, so with five search hits from five
sections, one rare word plus one common one was enough to move to another page, and near-ties flipped
back and forth (gated only by 1.2 s and a 1.2× margin). Small talk ("great, let's move on to the next
topic") moved it too.

How it works now (`frontend/src/lib/follow.ts`), in two steps because the two mistakes cost differently:
- **Which passage** (a candidate chunk). Evidence per passage = distinctive words of the last 30 spoken
  content words, each counted once, fading with a 12-word half-life; a word's weight is its rarity across
  the candidate sentences, discounted when most passages contain it. A passage takes over only when its
  evidence is ≥ 7 from ≥ 3 different words, ≥ 1.6× the current passage's (+1) and ≥ 1.35× every other
  passage's, the lead holds for 700 ms, and the current passage has been shown ≥ 2.5 s. Search hits get a
  slight rank prior; a passage opened with read_passage a 1.15 boost. When nothing qualifies the page
  stays where it is.
- **Which sentence** inside the passage: the last 12 content words, recency-weighted, need ≥ 3 matches
  and a score ≥ 3, with a forward pull (next 1–3 sentences ×1.2, backwards ×0.75), a 1.25× margin over the
  current sentence and ≥ 1.2 s between moves (a re-check keeps the last move of a burst).
- New lookups keep the current sentence when it's among the new results; a new exchange clears the
  evidence but not the highlight. More conversational words are stop words; topic words ("learning",
  "understanding") deliberately aren't.

Tuned against an independent judge, not by feel: every tutor answer in a real 150-turn session (148
answers, 93 retrieved passages) is replayed at speaking pace with a fake clock, and each spoken sentence is
labelled with the candidate sentence EmbeddingGemma finds closest (sentence-similarity prompt, labels
only above 0.88 similarity, spot-checked by hand). Old → new:

| | old | new |
|---|---|---|
| moves to the wrong place | 60 | 33 |
| moves while the tutor wasn't using the sources | 27 | 12 |
| jumps of more than a page | 38 | 5 |
| back-and-forth (A→B→A) | 16 | 1 |
| passage right / sentence ±1 | 90% / 77% | 90% / 76% |

Results hold at 1–8 words per transcript piece and 2–3.5 words/s. Most remaining "wrong" passages are
places where the notes say the same thing twice (definition on p3 and p41), where staying put is the
better behaviour. Tried and rejected: word-pair evidence (more far jumps, no accuracy gain) and committing
a pending switch when the answer ends (8 commits, 7 of them wrong). Verified live: a two-topic answer
moved p5 (tank sizing) → p6 (maintenance) once, and a chapter explanation walked p3 → p4 in order.

## 25. Offline or online embeddings (2026-10-11)

Some computers can't run EmbeddingGemma (see §21). `EMBEDDING_MODEL` in `.env` picks the embedder: `offline`
(default, EmbeddingGemma in its crash-isolated process) or `online` (Gemini Embedding 2 through `GEMINI_API_KEY`;
`tutor/retrieval/online.py`). Anything else is an error shown in the API window and Settings. Command-line tools
use the same setting (`create_embedder()`).

**Online embedder, from the API's measured behaviour (not assumed):**
- A list of plain strings is merged into ONE embedding (the model is multimodal); each text is sent as its own
  `Content`, which returns one vector per text, identical to embedding it alone (checked: cosine 1.0).
- At most 100 texts per request (`BatchEmbedContentsRequest`); `task_type` isn't supported, so the task goes in the
  text, in the same format as EmbeddingGemma (`title: … | text: …`, `task: search result | query: …`, Google's
  documented format for this model). `output_dimensionality=768`, renormalized by the API (and again by us).
- Quota is counted per text: the free tier allows 100 a minute (`EmbedContentRequestsPerMinute…`). A client-side
  sliding-window limiter keeps under it (`VOICE_TUTOR_ONLINE_EMBED_PER_MINUTE`), leaving 5 a minute for live
  questions while indexing. A 429 is retried after the API's `RetryInfo` delay; a per-day quota is reported straight
  away with what to do; 400/401/403/404 are explained (bad key, model unavailable); network errors and 5xx retry
  with backoff. `EmbeddingError.transient` separates "may work later" from "fix the setup": at startup one tiny
  check request turns a wrong key into a clear error, while no connection only warns.
- Chunking still counts tokens with EmbeddingGemma's tokenizer (`tokenizer.json` only, 32 MB, public, no torch:
  online mode never imports torch or downloads the model). Gemma shares Gemini's tokenizer, so chunk sizes fit.

**Two vector spaces, no mixing.** The two models' vectors aren't comparable, so a document keeps one LanceDB table
per embedding space (model, dimension, prompt version): the same chunks in the same order, vectors from one model
each. `meta.json` lists them in `vector_indexes`. Opening a document picks the current embedder's table; a search
embeds the question with the current embedder and searches only tables in that space, while keyword (BM25) search
runs on every document. Chunk-level settings (chunker/index version) still force a full re-parse; the embedding
model no longer does.

**Switching modes.** On startup the server lists documents missing the current space and queues a background
"reindex" job for each (`add_embeddings`: embeds the stored chunks, no re-parse; progress, orders and citations are
unchanged). The job shows in every notebook that has the document ("Updating search · 40 of 115"); meanwhile that
source is `keyword_only` and the search tool tells the tutor its results for it are keyword matches. Uploading a PDF
that's already indexed in the other space only adds vectors. A failed job (e.g. daily quota) stays visible with its
reason; dismissing it retries. If the question can't be embedded (no connection, quota), search falls back to
keywords and says so instead of failing. Switching back is free, since both spaces are kept.
Documents indexed before this change (one `chunks` table, model in `embedding_model`) read as an EmbeddingGemma
index with nothing to migrate; adding a second space rewrites their metadata in the new format.

**Verified:** 20 unit tests (fake client and embedders: batching, one vector per text, 429/daily/bad key/network,
the limiter's headroom, space switching with progress kept, keyword fallback, legacy layout, the server's background
job). Live against the Gemini API in a throwaway data folder: indexed a PDF offline and marked 64%, restarted
online (ready in 3.2 s, no model loaded); the server re-embedded its 11 chunks in the background, progress stayed
64%, search returned the same top results; a new upload online was indexed in the online space only. The real
library (offline) opened unchanged with no re-indexing.

## 26. Hosting: frontend on Vercel, API on a VPS, with login (2026-10-11)

One codebase; hosting is switched on by settings only, and `run.bat` behaves exactly as before.

**How they talk.** The browser loads pages from Vercel (`tutor.edgedrop.app`, functions pinned to Mumbai with
`frontend/vercel.json`) and calls the API directly at `api.tutor.edgedrop.app` (Caddy :443 on the VPS, the API on
127.0.0.1:8766): REST with `credentials: "include"`, PDF uploads, page images, and the voice WebSocket. So none of
it is subject to Vercel's 4.5 MB request or 5-minute connection limits. `NEXT_PUBLIC_API_URL` (build time) is the
only frontend setting (`lib/api.ts`: `apiUrl`, `apiWsUrl`); unset, everything is same-origin `/api` through the
Next.js rewrite as before. Server-rendered pages fetch from `VOICE_TUTOR_API_URL` (default: the same) forwarding
the visitor's cookies, and redirect to `/login` on 401.

**Login** (`tutor/auth.py`, `tutor/server/auth_routes.py`, `VOICE_TUTOR_AUTH=on`). One email + password account,
created on the server (`manage.py user create`); no sign-up page; resets by command. Argon2id (19 MiB, t=2, p=1:
OWASP's minimum, light enough for 1 GB), rehash-on-login. Sessions: 256-bit random tokens, only their SHA-256 in
SQLite (WAL); 30 days idle / 90 days absolute; logout, sign-out-everywhere and password changes end sessions.
Unknown email and wrong password give the same message and cost the same work (a dummy hash). Failures are
throttled per email (5 / 15 min) and per IP (20 / 15 min) and logged with the IP. The cookie is HttpOnly, Secure,
SameSite=Lax, `Domain=tutor.edgedrop.app` (so `api.tutor.` gets it and the rest of `edgedrop.app` doesn't), named
`__Secure-vt_session`, Max-Age 90 days with the idle limit enforced server-side.

**Request security** (`tutor/server/security.py`, one ASGI layer for HTTP and WebSocket):
- Origin check, always on (locally too): POST/PUT/PATCH/DELETE and WebSockets from a site that isn't the local UI
  or a configured frontend get 403. This also stops CSRF against the login-free local app.
- Every `/api` route needs a session except health and the sign-in endpoints; the WebSocket is checked before it's
  accepted. CORS: Starlette's middleware with the allowed origins read per request (`ConfiguredCORS`), credentials
  on, no wildcard; 401s are labelled too, so the browser can see them.
- nosniff, DENY framing, strict referrer, `no-store` on sign-in responses; at most `VOICE_TUTOR_MAX_LIVE_SESSIONS`
  voice sessions; upload cap `VOICE_TUTOR_MAX_UPLOAD_MB`.
- Fail closed: the API won't start without login if it's reachable by others (public origins set, or bound to a
  non-loopback address); an unrecognised `VOICE_TUTOR_AUTH` value means login on.

**Frontend security.** `src/proxy.ts` (Next 16's middleware) sets a per-request CSP with a nonce: scripts only from
self + nonce (no `'strict-dynamic'`, which would stop `'self'` covering the mic's audio worklet), connections only
to self and the API, images and media from self and the API, `frame-ancestors 'none'`, no objects. Styles need
`'unsafe-inline'` (React style attributes); a style nonce would cancel it, so none. The theme script carries the
nonce (the layout reads `x-nonce`). Static headers: nosniff, DENY, referrer policy, Permissions-Policy (microphone
self only), HSTS when hosted. The session cookie is HttpOnly, so even injected script couldn't read it.

**Server** (`deploy/vps/`). `setup.sh` (idempotent): 2 GB swap, Python 3.13 via uv, the server-only dependencies
(`requirements-server.txt`: no torch, 747 MB venv vs 1.7 GB, versions from `requirements.lock`), a `voicetutor`
system user, a systemd unit (MemoryHigh 650M / MemoryMax 800M; ProtectSystem=strict, writes only to
`/var/lib/voice-tutor` and `/var/log/voice-tutor`, no capabilities, no new privileges), Caddy with automatic
certificates (Cloudflare record grey-clouded: its free certificate doesn't cover two-level names), iptables rules
for 80/443 saved with netfilter-persistent (Oracle's image rejects everything but SSH; the VCN security list must be
opened too), and a nightly backup timer. `voice-tutor` wraps status/logs/restart/update/backup and `manage.py`
(run as the service user with the service's settings). `VOICE_TUTOR_LOGS` moves logs off the read-only code tree.

**Data** (`manage.py`). `data info`; `data prune` removes PDFs no notebook uses, unfinished uploads and temp files
(refuses while the API runs; `voice-tutor data prune` stops and restarts it); `data backup` writes a tar.gz without
caches, with the login database copied through SQLite's backup API so it's consistent while the server runs.

**Verified.** 25 security tests (account rules, hashing on disk, enumeration, expiry, throttling, login-required
routes, origins, CORS, the WebSocket, headers, cookie scope, fail-closed startup, local mode unchanged) and 4 for
`manage.py`; 115 tests in all. The server-only install was built and tested on Linux (WSL, Python 3.13.11):
98 tests pass without torch. Live, on this PC: the local app (headers, CSP with the worklet, theme script, PDF
pages, a voice reply) unchanged; and the hosted setup simulated with `tutor.localhost` + `api.tutor.localhost`
(the frontend built as Vercel builds it): signed-out redirect, wrong-password message, sign-in, the cookie shared
across both hosts and invisible to scripts, page images and the voice WebSocket across origins, an online-embedding
search answer, sign-out and sign-out-everywhere, return-to-page after sign-in, and `next=//evil.example` refused.

## 27. Simplified progress, upload feedback, phones and audio (2026-10-11)

**Completion tracking removed** (supersedes §23 at the person's request). No percentages, rings, ticks, section
statuses or marking anywhere: the `update_progress` tool, `POST /api/docs/{id}/progress` and `/reset`, the outline
menu items, the chat chips, and the prompt's "(completed)" annotations are gone. What stays is the reading
position (`Lesson.read`/`position`), which is how lessons resume ("continue", "part 2 of 5"). `progress.json` keeps
only the cursor; older files with read/done lists load and drop them on the next save; a cursor past the end of
the document starts over. Old history entries with progress chips are filtered out when shown.

**Upload feedback.** Measured on the VPS: sending a 6.7 MB PDF took 15-18 s and a 14 MB one 42 s, during which
nothing was shown (the job only existed once the upload finished); reading a PDF took ~100 s on the 1/8 CPU; and
when a job finished its row vanished a moment before the source appeared. Now uploads go through XMLHttpRequest
(fetch can't report upload progress) and a row appears the instant files are picked: "Uploading… 45%", then
"Upload sent · starting…", replaced in place by the real job(s). The server reports how long the current stage has
run (`stage_seconds`), shown as "Reading the PDF… 1:12". When a job finishes, the notebook (with the new source) is
loaded first and the row removed after, so there's never a gap. Verified with a recorded DOM timeline.

**Phones** (checked at 360 and 375 px with touch emulation): no horizontal overflow on any screen; controls that
only appeared on hover (notebook menu, remove source, copy) are always shown on touchscreens, with 34 px touch
targets; inputs are 16 px on touch devices (iPhones zoom into smaller ones); dialogs size to the visible viewport
(`dvh`); the continue card stacks with a full-width Resume; Settings hides keyboard shortcuts on touch and wraps
long values; pinch zooms the PDF (two-finger handler in `pdf-view.ts`, `touch-action: pan-x pan-y` so the browser
doesn't zoom the app); no tap highlight flash. **Screen stays on** while the app is open on a phone (Screen Wake
Lock API, re-taken when the page becomes visible again or on a tap; desktops unaffected).

**Beeps and "sirens" on a phone.** Likely causes found in the audio path: (1) on a phone speaker the mic hears the
tutor, Gemini's voice activity detection takes that as the person speaking and interrupts its own reply, again and
again; each hard stop of the audio clicks, and the native-audio model hearing itself can produce odd tones;
(2) interruptions stopped the audio mid-waveform; (3) a chunk with an odd byte count would misalign every later
PCM16 sample (loud noise). Fixes: Gemini's start-of-speech sensitivity is LOW (`VOICE_TUTOR_VAD_START_SENSITIVITY`):
a clear voice still interrupts, echo and noise don't; interrupted replies fade out over 40 ms on their own gain
node before stopping; odd trailing bytes are carried into the next chunk. Interruptions are now logged per session
("Voice session ended after 2m30s, 4 exchanges, 1 interruption"), so a real phone session shows whether echo still
cuts in. It did: one phone-speaker session had 13 interruptions in 2.5 minutes, each "heard" as a one-syllable word
("bed", "pet", "net", "Bett"), so the tutor kept restarting and the person's real words got lost between them. Gemini
commits to "the person started talking" after a configurable stretch of speech (`prefix_padding_ms`, never set
before); it's now 300 ms (`VOICE_TUTOR_VAD_PREFIX_MS`), longer than those bursts and shorter than "wait, wait".

## 28. The library, dark theme, tooltips and touch (2026-10-11)

**Home is a library.** No hero logo and no icon cards: a quiet header ("Your library", counts, search, New notebook),
a "Continue reading" card with the book you're in and the exact section and page, and a shelf where every notebook is
a 3D book standing on a soft plinth with its name, PDFs/pages and position underneath, plus a blank "New notebook"
book. An empty library shows one blank book and what the app does, in words. Long-press (touch) or right-click a book
opens its menu (Open, Cover colour, Rename, Delete).

**One system** (second pass, after review): every book on the page stands on the same plinth (one radius, one
colour, one size: 192px tall, books 118×144 on desktop; 158px / 100px on phones), every section uses one heading
style ("Continue reading", "Notebooks"), and captions line up across the shelf. "Continue reading" is no longer a
large mostly-empty card that repeated the shelf: it's the book on its plinth with the notebook title, where you are
(the section and the part inside it, not the whole breadcrumb), the page and which PDF, and Resume. "New notebook" is a
normal shelf item: a dashed book exactly the size of a real one (the plus drawn in CSS) with a caption, so the row
aligns. Search and the header button share a height (38px). Cover titles scale with their length (short ones larger).

**Books** (`components/ui/book.tsx`): a port of the Vercel-style Book component to this project's CSS (the project
has no Tailwind/shadcn, and adding a second styling system for one component would fight the design tokens; the
original also needed a missing `useResponsive` hook and an external texture the CSP blocks). Same construction: cloth
cover with binding shading, a page edge rotated 90° and a back cover, in 3D with container-query units; it turns
on hover (mouse only) and presses on touch. Title in the display serif, a small rule and the PDF count.

**Cover colours** (`lib/covers.ts`, `tutor/notebooks.py COVERS`): twelve muted book-cloth tones, each with a title
ink that reads on it. Colours from a hash alone collided (two of two real notebooks landed on the same one), so a
notebook stores its cover: new ones get the least-used colour (all twelve before any repeats), older files get one
oldest-first on first load and keep it; `PATCH {cover}` changes it (picker with a live preview). Each PDF also has a
colour from its content hash, shown as its spine in the sources list (the active one ringed), so a file looks the
same everywhere. The notebook header shows the notebook's cover (click to change it) instead of an icon tile.

**Dark theme**, redesigned as warm graphite instead of near-black: background #131312 < surface #1b1b19 < raised
#20201e/#282826, so panels separate by lightness; warm off-white ink (#eeece7) and muted text with real contrast;
borders and a faint highlight replace shadows that disappear on dark; inverted tooltips; book shadows deepened.

**Tooltips** (`lib/tooltip.ts`): the CSS `::after` bubbles were clipped by scrolling panels, ran off the window
edges, set `position: relative` on buttons that needed their own positioning, and stuck after a tap on touch. Now one
fixed-position bubble on the body, placed from the element's box (below, flipping above when there's no room;
`data-tip-side="top"` to prefer above), clamped 8px inside the window, shown after 450ms on mouse/pen hover (instantly
when moving between buttons) or at once on keyboard focus, hidden on scroll, press, Escape, blur or leaving the window,
and never on touch. Verified: the top-right Settings tooltip shifts left to stay on screen; touch shows none.

**Touch.** `touch-action: manipulation` on controls (no double-tap-zoom delay); sticky :hover looks undone on touch
browsers and replaced by pressed states (buttons scale, rows tint, books press in); hover-only controls always shown;
long-press menus on books; no callout or text selection when long-pressing a book.


## 29. Reading PDFs in a separate process (2026-10-12)

The API server grew with every PDF it read and never shrank. pymupdf4llm reads a PDF with neural layout models
(ONNX), and the process that ran them keeps 80-120 MB per document afterwards. Measured on the 1 GB server, one
process reading three of the notes PDFs: 290 MB loaded, then 410, 488 and 579 MB; `malloc_trim` got back only
62 MB. The live API had reached ~590 MB (mostly in swap), heading for its 800 MB hard limit, where systemd kills it.

Upload jobs now read the PDF in a short-lived child process (`tutor/ingest/isolated.py`, started with "spawn" like
the embedding worker) that runs exactly `parse_pdf` and sends the document back; chunking, embedding and saving stay
in the server. Same measurement with the change: 308 MB loaded, then 218, 214, 214 MB. The documents are identical
(compared section by section and paragraph by paragraph for the test PDFs and the notes PDFs, on Windows and on the
server); errors come back as the same exception with the same message. A child that dies (out of memory, a native
crash) fails that job with an explanation instead of taking the server, and any voice session, down with it; the
server notices a dead child by checking it's alive, not only by its pipe closing (on Windows a child that dies very
early never closes it). Cost: a few seconds per PDF to start the child, against 1-3 minutes of reading. The
command-line tools (`ingest.py`, `console.py`) are short-lived and still read in-process.

## 30. Follow-along removed (2026-10-12)

Removed at the person's request (supersedes §19 and §24): moving the page to match the tutor's speech, from
passage and sentence matching alone, kept jumping between sections and couldn't be made dependable. Gone: the
tracker (`lib/follow.ts`), the "Follow along" switch and the "Back to the tutor" pill, the viewer moving or
highlighting by itself while the tutor talks (including opening a passage's page when the tutor starts reading
it), reporting hand-scrolling from the viewer, and the full passage text sent to the browser with tool results
(`ui.text`; the model still gets the passage). The page now moves only when the person asks: a citation chip
(scrolls to the passage and highlights it, through `/api/docs/{id}/locate`, which stays for this), an outline
section, a source, the page buttons. Captions and the floating bubble still follow the audio as it's heard.

## 31. Local-run hardening, tutor prompt and voice fixes (2026-10-22)

A review pass before the Kritical Hacks 2026 demo, where the app runs entirely on one Windows PC. Server and launcher:
- Doc ids are validated (ASCII letters and digits, with a stored index) on every `/api/docs` route before any path
  is built from them, and removing a source checks it's in the notebook, so a crafted id can't read, write or
  delete outside the data folder.
- A batch upload is checked file by file before any job is queued: one bad file fails the request and queues
  nothing, instead of leaving the good ones indexing unseen.
- An unreadable notebook file is skipped (logged once) instead of breaking the library list and "New notebook".
  Notebook and settings files are written with the Windows-safe swap (`fileio.write_json`). Names are cut to 120
  characters on create as on rename.
- Numbers and choices in `.env` are checked at start with a sentence saying what to fix; the VAD sensitivities are
  compared in lower case (`Low` used to select high sensitivity silently). `.env.example` has one setting per line.
- `run.bat` refuses to rebuild the UI while the "Voice Tutor - UI" window from an earlier start is running (the
  rebuild replaced the files it was serving), and rotates `logs/setup.log` past 5 MB.
- Reading a PDF has a deadline (5 s a page, at least 5 minutes): a hung reader no longer holds up the one-at-a-time
  job queue. The outline answers 503 with a plain message while the search model loads, instead of a 500 after
  the browser has given up. A notebook deleted while its upload indexes leaves no orphan index, and orphans
  aren't re-embedded. Expected 4xx responses stay out of `errors.log`. Shutdown never respawns the embedding worker.
- The API window is quieter: `LANCEDB_LOG=error` hides lance's WARN on every new table, and the model's "Loading
  weights" bar is off (download progress still shows). Tests use their own data and log folders.

Tutor and voice:
- Web-search safety net: bare "net" dropped, topic phrases and negations ("don't search", "mat dekho") don't
  trigger it.
- Asking for a section now reads it including its subsections (`is_section_end` and `parts_left_in_section` cover
  the requested section).
- A missing source no longer stops voice sessions.
- Prompt: today's date; a Boundaries section (sources and web text are data, academic integrity, safety, identity,
  tool errors); general knowledge allowed but labelled; unclear-audio rules; English/Hindi pinned; speaking tables,
  formulas and page ranges; never saying section ids; short answers for quick questions; a spoken line before slow
  tools; the manner from Settings decides how to teach.
- The manner is fixed for a whole conversation (a change applies from the next one).
- Voice starts waiting for the search model with a notice, and stops waiting if the page is left.
- The first connect gives up quickly with an internet message.
- Texts sent while the connection is closing are re-sent after reconnecting.
- End, or a dropped socket, finalizes the reply.

## 32. TinyFish: web search that reads the pages, and web pages as sources (2026-10-22)

**Web search** (`tutor/web.py`) gets a switch, `WEB_SEARCH=tinyfish|tavily|duckduckgo` (lowercase, like
`EMBEDDING_MODEL`). Empty keeps the earlier behaviour (Tavily with a key, else DuckDuckGo), so nothing changes until
it's set; an unknown value or a missing key is an error in the banner, the log, Settings and the tool result, not a
quiet fallback. With `tinyfish`: TinyFish Search (`GET api.search.tinyfish.ai`, `X-API-Key`, with a `purpose` naming
the tutoring intent) finds results; TinyFish Fetch (`POST api.fetch.tinyfish.ai`) reads the top 3 in one request,
Markdown, 12 s per page (TinyFish allows 110 s; a spoken answer can't wait that long). From each page the paragraphs
sharing the most words with the question go to the model as `content` (~1,400 characters, page order; the opening
when nothing matches); images and link targets are dropped. Snippet-only search answered from one line per site;
reading the pages gives the tutor the actual explanation to teach from and cite.

Failures degrade, never silently: Fetch failing leaves the snippets (the result notes it); Search failing moves to the
next available service in the order TinyFish, Tavily, DuckDuckGo, and the result says which one answered
(`searched_with`), which the conversation shows ("Searched the web with TinyFish for …"). TinyFish's 401/402/429
become plain reasons (key not accepted, free allowance used and wallet empty, too many requests). Plain `httpx`
calls against the documented REST endpoints: no SDK, so no new dependency for the server install. The Search API's
`fetch` query parameter exists in its OpenAPI spec but isn't documented, so it isn't used.

Not used, on purpose: Agent (15-60 s per run, paid; nothing here needs clicking through sites), Research (5-20 min per
report, 5 a day: far too slow for conversation), Browser and Monitor (no fit). Limits per the docs on 2026-10-22: Search
12,000 and Fetch 1,000 pages free per day (reset 00:00 UTC), then wallet credits; 30 searches and 150 pages a minute.

**Add from a link** (`tutor/web_source.py`, `POST /api/notebooks/{id}/sources/link`). A link straight to a PDF is
downloaded (any `WEB_SEARCH`; the upload size limit applies). A web page needs `tinyfish`: Fetch returns clean HTML
(rendered in a real browser), which PyMuPDF's Story lays out as an A4 PDF; heading positions recorded during layout
become its bookmarks, so the existing parser takes the outline from them (`structure_source` "outline"). Everything
after that is the normal PDF path (child-process parse, chunks, embeddings, page images, citations), so a web page is
taught, searched and cited by page like any PDF, and its first line names the address it came from. The layout engine
writes ligatures ("ﬁ"); the parser and the citation locator both normalise them, so search isn't affected. Scripts,
styles, navigation, images and iframes are dropped, and attributes are stripped inside tags only (never the page's
text); forms are kept, since some sites wrap the whole page in one. The server fetches only public addresses: every
hop of a redirect is resolved and refused if any address isn't global (localhost, private ranges, cloud metadata),
since on the hosted server a link must not reach the machine's own services. Jobs show a "Getting the page…" stage;
TinyFish's per-page errors (blocked, login wall, not found, timeout) become readable job errors.

Tests: `tests/test_web.py` (the switch, Search + Fetch requests and results, every fallback, excerpts) and
`tests/test_web_source.py` (address checks, downloads and redirects, HTML to PDF with its outline through the real
parser, TinyFish errors), with TinyFish faked from its documented response shapes; `tests/test_server.py` adds a web
page through the endpoint end to end (indexed, outline from its headings, found by search).

## 33. Tutor behaviour from research, and web search that finds better pages (2026-10-22)

**Research** (three parallel reviews, primary sources): Google's LearnLM principles and its "Towards Responsible
Development of GenAI for Education" report; OpenAI's study mode and Anthropic's learning mode; Bastani et al. (PNAS
2025: an answer-giving tutor raised practice scores but lowered exam scores by 17%; a hint-giving one didn't); Kestin
et al. (Harvard physics RCT: structured AI tutoring beat active learning); Tutor CoPilot (more probing questions,
less generic praise); retrieval practice, worked examples and fading, refutation of misconceptions, process praise
(Dunlosky, Roediger & Karpicke, Renkl, Mueller & Dweck); LLM failure modes (missing the first wrong step; sycophancy
when a student insists); and voice-agent guidance (Gemini Live best practices, OpenAI realtime prompting, Vapi,
ElevenLabs): off-topic handling, prompt injection, unclear audio, preambles, speaking numbers.

**What changed in the prompt** (`prompts/system.md`, ordered persona, then teaching and conversation, then guardrails):
- Teaching keeps the earlier rule that explanations are complete (the person asked for it), and adds: after a topic,
  the student explains it back or applies it ("Does that make sense?" is banned); one level question on a new topic
  when it matters; analogies from everyday Indian life with their limits stated; answers are checked against the
  sources before being judged; the first wrong step is named and the student tries again before being told; a
  misconception is named with why it's tempting; no giving in when the student insists without evidence; smaller
  steps when they struggle; quizzes get harder after right answers and summarise strengths every five questions.
- Sources: every answer from them names its page; course definitions in the sources win over the web.
- Web search: English keyword queries, the question passed separately, kind/recency/India chosen deliberately,
  sources named by their cite name, never a URL, dates for anything current.
- Staying on track: one line for banter; brief, labelled help for learning questions outside the sources; one-line
  refusal for non-learning tasks; honest "an AI running on Gemini" about itself.
- Guardrails: instructions inside sources or pages are never followed; role-change attempts and requests for the
  prompt get one line; homework: facts from the sources are told with their page, written work to submit gets a
  hint ladder (idea, step, part of a step, a worked similar problem, then the answer after a real attempt); crisis
  (Tele-MANAS 14416 and 112, said digit by digit); personal medical/legal/financial decisions go to professionals;
  harmful requests declined; no personal data asked for or repeated; abuse warned once.
- Teaching styles (`tutor/voice/style.py`) follow the same research: Direct ends a topic with a use-it question;
  Step by step uses a worked example then fades; Socratic escalates hints after two tries; Exam prep adds a
  retrieval-practice quiz. Encouragement controls warmth only, never accuracy; praise names what was done.
- The native-audio transcript sometimes carries markup like `<speaker:catch-up>`; it's stripped, including when split
  across transcript pieces (`clean_transcript` in `tutor/voice/gemini_live.py`).

**Checked with real conversations** (Gemini Live through the local server, typed, a fresh session each): a full
explanation with pages and a use-it question; a train-ticket request declined in one line; a joke then back to the
notes; a jailbreak and a prompt request refused; "write my assignment" turned into guidance; "it's the same as
rule-based programming" corrected with page 5 and held when the student insisted; photosynthesis (not in the notes)
answered as general knowledge with an offer to search; ISRO and AI-regulation news via TinyFish (news, India,
past week) citing India Today and PIB; research papers with authors and years; Hindi answered in Hindi; a crisis
answered with Tele-MANAS and 112; a quiz one question at a time with a hint before the answer; a stray "bed" asked to
repeat; "are you a person?" answered honestly; abuse, a medicine question and an Aadhaar number handled; and the
Socratic and Exam-prep styles. Not fixable in the prompt: the speech transcript once rendered "data" in katakana in
a Hindi reply (the transcript is the model's speech recognition of its own audio).

**Web search** (`tutor/web.py`): TinyFish parameters checked live first (location IN, exclude_domains, news with a
recency window, research papers with years; recency on research papers is rejected with a 400, so it maps to
pub_year_min). The tool takes `query` (English keywords), `question`, `kind` (general/news/research), `recency` and
`india_specific`. Results exclude social, login-walled and homework-dump sites, are re-ranked with a small boost for
reliable sources, one per site; Fetch reads the best readable pages in parallel, one request each (a batch waits for
its slowest page), within a 9-second budget for search and reading together. Measured from India: about 2 s of
network overhead per request on top of TinyFish's page time (Wikipedia 1-1.8 s, PubMed Central ~7 s), so pages get 6
s each; sites that always block or return a shell (ScienceDirect, IEEE Xplore, PubMed Central, Khan Academy, ...)
aren't read but stay as sources; readable pages from the wider top 10 are preferred. Excerpts weight rare words
(IDF), carry their nearest heading, and drop menus and instruction-like text; a page with nothing relevant gives no
excerpt. Results carry `cite` names to say aloud, never an address (the publisher, else the site name at the end of the
page title, else the domain's main word); the same search within 10 minutes is answered from memory. Tavily and
DuckDuckGo fallbacks honour news and time windows. Highlights (Fetch's ranked passages) would replace the local
excerpts but isn't enabled on this account (403). Typical result: 2-3 pages read, 6-9 s.

## 34. The tutor can switch the PDF on screen (2026-10-22)

"Change the PDF", "switch to unit 2", "dusri PDF kholo", "show me page 12": the `open_source` tool resolves the
source (by number or title words, like the other tools), makes it the active source (saved, so the left panel marks
it as Teaching) and returns its top-level sections and the reading position, so the tutor can say what's there and
ask what to cover. It doesn't read or move the reading position: teaching from it is still `read_passage`. A page
outside the PDF is a speakable error. The browser gets `{"kind": "open", doc_id, page}`: the left panel switches the
active source and reloads its outline, and the viewer opens that PDF at the page (where reading left off when no page
is given) and is revealed. This is the one time the viewer moves for the tutor, because the student asked for it;
passages it reads don't move the viewer. When `read_passage` itself changes source, the outline is reloaded too
(before, the left panel highlighted the new source but kept the old outline). Checked live: switching to unit 2,
page 5 of it, back to unit 1 then teaching its first topic, and "Dusri PDF kholo"; and in the browser, both panels
followed "Switch to the first PDF and show me page 4".

## 35. Replies that didn't come: the end-of-speech setting (2026-10-22)

**Symptom**: sometimes after one reply, the next thing said got no answer for many seconds (14 s in one session
at 13:33 on 2026-10-22, with the laptop's mic array). The log showed the audio reaching the server and Gemini; the
browser was running and visible, no stalls; Gemini sent the transcript of what was said only when it finally
answered. So Gemini hadn't decided the person had finished.

**Measured** (a recorded question streamed to Gemini Live at real time, then 25 s of what the mic would send after
it; `scratchpad/vad/vad_test.py` in the session that did this): with digital silence after the question, replies
start in about 1 s. With faint room noise (-60 to -50 dBFS of white noise) and end-of-speech sensitivity **low**
(the setting then), 3 of 16 replies took 4.2-7.6 s; with **high**, 0 of 26, all 0.6-1.0 s. Louder noise (-45 to
-35 dBFS) didn't cause waits with either. Questions with a 0.6 s or 0.9 s pause mid-sentence were never cut off with
either setting (the 1 s `VAD_SILENCE_MS` governs pauses). So the default is now **high**
(`VOICE_TUTOR_VAD_END_SENSITIVITY`). Checked end to end through the local server: replies 1.4-2.0 s after speaking,
including a search of the PDF.

**Evidence next time** (`MicMeter` in `tutor/server/live.py`): the server measures the mic level of the audio it
forwards (the room's level, and when the person's voice stopped; the tutor's own voice coming back is ignored while
its reply plays, and digital silence isn't the room). Each reply's log line now says how long after the person went
quiet it started, and the room level ("Reply audio started 0.50s after your input (you went quiet 2.1s before; room
-55 dBFS)"); and when someone talked, went quiet for 5 s and nothing came back (no reply, no tool call), a warning
says so with the room level. A loud room means noise kept Gemini waiting; a quiet one points at the connection.

## 36. Teaching a section: one part at a time, and no stopping halfway (2026-10-22)

**What went wrong** (14:07, "So let's just start from the very beginning"): the tutor read five passages in 13 seconds
(cover, syllabus, then the three parts of "What is Cognitive Computing?"), said only filler between them ("Continuing
on, let's explore…"), never explained any of it, and asked a question about it. The rule then was "keep calling
read_passage with target next until is_section_end", explaining as it reads only for sections of more than three
parts (and on main: gather everything, then explain), so it collected and never taught.

**Now:**
- The prompt and the read_passage description say: read a part, teach every point in it out loud, then read the
  next, without asking, until the section ends; a cover, contents or syllabus gets a sentence; "start from the
  beginning" means the overview then the first topic, in the same turn; never end a turn announcing a step.
- Each passage carries `next_step`: teach this part now, then read on (or, at the end of the section, ask them to
  explain it back).
- `TeachingGuard` (`tutor/server/live.py`), within one reply (anything the person says clears it):
  - reading on before the part just read was taught (under 200 characters said since, for a part of 400+
    characters) gets "teach the part you just read first" instead of the next part;
  - jumping away from a section with parts still to come (it once went from the first part of a section to the
    next section, skipping the analogy and the memory trick) gets "N more parts: read on and teach them";
  - a turn that ends announcing a step it didn't take ("Let's dive right into the first topic.") or stops mid-section
    without a question gets a nudge to carry on (like the web-search nudge);
  - short parts and front matter (contents, syllabus, index…) aren't held; at most two holds in a row and four
    nudges per thing they said, so it can't stall or loop.

**Checked live** (typed, Gemini Live through the local server, a fresh session each): "start from the very beginning"
eight times and "teach me the section on what is cognitive computing" three times. In every run after the fix the
tutor taught the definitions, the calculator / medical intern analogy and the memory trick with their pages, then
asked them to explain the difference in their own words; "Continue" went on to the three eras. Runs where the model
still stopped mid-section or after announcing a step were picked up by the nudge and finished the section.

Later the same day: asked to "explain the three principles", the tutor read the three principle sections (each a
one-part section) back to back with only "Let's look at the next principle" between them, then explained all three.
The guard now holds back reading anything (a jump to another section included, not only "next") while the part just
read hasn't been taught; re-reading and going back are still allowed. Checked live: the read of the third principle
was held, the tutor explained the first two, then read and taught the third and asked a question.
