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
| "Where were we?" | **Guide** | Position, outline, what's covered |

**Non-negotiable behaviours**
1. **Sources first**: topic questions start with `search_sources`; pages spoken naturally.
2. **Web second, labelled**: "Your document says… / A current web source, <site>, says…".
3. **Honest gaps**: "Your sources don't cover that — want me to look it up?"
4. **Remembers**: per-PDF reading position + covered sections, per-notebook conversation history.
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
docs/<doc_id>/progress.json        cursor, covered chunk orders, last summary
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
language (Hindi/English mixing mirrored), never talk over the user (3.8 proactive audio is always on).

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
  actions, composer, voice dock) · PDF viewer (continuous vertical scroll; follows teaching, highlights
  passages, opens PDF). Draggable splits between the panels; full-screen reading mode (§18).
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
  src/lib/         api (browser), server-api (server rendering), live, pdf-view, fluid-orb, follow,
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
  `start "Voice Tutor - Server" cmd /k call .venv\Scripts\python.exe app.py`.
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
  swap in without flashing. Citation/follow-along scrolls to the page and the highlighted passage.
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

## 19. Follow-along (2026-10-10)

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
- **Notebook page:** `controller.ts` holds all behaviour (voice session, follow-along, splits, reading mode,
  uploads, keyboard) and exposes a small external store; `NotebookView.tsx` renders from it. Imperative
  engines (PdfView, FluidOrb, LiveClient, FollowTracker) are the same code as before, typed. On open, the
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

## 24. Follow-along v3: passage first, then sentence (2026-10-10)

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

