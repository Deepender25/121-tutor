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
Browser (web/)                         Local server (tutor/server, FastAPI)                 Google
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

## 8. Web app (web/, no build step)

- Plain ES modules + one stylesheet; no Node toolchain needed. `run.bat` → server window → `app.py` → uvicorn.
- **Home**: notebook grid (create / rename / delete).
- **Notebook**: Sources panel (upload with progress, active source, outline with progress ticks — click
  a section to have it taught) · Conversation (transcript, tool activity chips, citation chips, quick
  actions, composer, voice dock) · PDF viewer (continuous vertical scroll; follows teaching, highlights
  passages, opens PDF). Draggable splits between the panels; full-screen reading mode (§18).
- **Voice mode**: WebGL **FluidOrb** (shader from the user-provided component, ported to vanilla JS) in
  the dock and as a large orb on a focused stage with live captions; colour by state (blue listening,
  violet speaking, teal looking things up, grey muted) and swirl speed by audio level.
- Light/dark/system themes; responsive down to phone width (tabbed panels); container queries for the
  chat column; reduced-motion respected.

## 9. Layout

```
app.py              server entry point (run.bat starts it)   run.bat   venv + deps + .env + server window
console.py          text console (debug)                     ingest.py · query.py   CLIs
prompts/system.md   conversation behaviour
tutor/
  config.py document.py lesson.py library.py logs.py notebooks.py tools.py web.py
  ingest/  parse.py chunk.py
  retrieval/  embedder.py store.py search.py
  voice/  base.py gemini_live.py prompt.py
  server/  app.py live.py jobs.py pages.py state.py
web/  index.html  css/app.css  js/{main,home,notebook,live,api,ui,fluid-orb}.js  js/audio/recorder-worklet.js
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
- **Tracking** (`web/js/follow.js`): candidates are split into sentences; the last ~18 content words the
  tutor has *spoken* are matched against them (IDF-weighted, recent words weigh more, a pull towards reading
  forwards, hysteresis + a 1.2 s minimum between moves, with a re-check so a burst's last move isn't lost).
  Replays of real answers follow sentence by sentence, e.g. the three eras of computing.
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

