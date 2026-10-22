# Voice Tutor

Talk one-to-one with your PDFs, like NotebookLM but as a live voice conversation. One partner that is
your **tutor** (teaches the material in order and remembers where you stopped), **explainer** (answers from your
sources with page references), **researcher** (checks the web and says which is which) and
**brainstormer**. Design and decisions: [DESIGN.md](DESIGN.md).

## Quick start (Windows)

Needs **Python 3.10+** (3.13 recommended) and **Node.js 20.9+** (the LTS from https://nodejs.org).

1. Double-click **`run.bat`**. It runs five steps in a small setup window:
   1. **Virtual environment**: creates `.venv\` in this folder (first run only) and checks it's valid.
   2. **Python dependencies**: installs everything into `.venv` (first run ~1–2 GB, a few minutes; again
      only when `requirements.lock` changes or packages are missing). Full pip output: `logs\setup.log`.
   3. **.env**: created from `.env.example` if missing; warns if the Gemini key is empty.
   4. **User interface**: installs its packages and builds it (first run about a minute; again only when
      something in `frontend\` changed). Output: `logs\setup.log`.
   5. **Servers**: two terminal windows with live logs, **"Voice Tutor - API"** (Python) and
      **"Voice Tutor - UI"** (Next.js), then the app opens in your browser at http://127.0.0.1:8765.
      If they're already running, it just opens the browser.
2. Put your free Gemini key in `.env` after `GEMINI_API_KEY=` (https://aistudio.google.com/apikey) and
   restart.
3. Create a notebook, drop in PDFs, tap the orb and talk. Or type.

### Offline or online embeddings

PDFs are indexed for search with an embedding model. Choose where it runs with `EMBEDDING_MODEL` in `.env`:

| `EMBEDDING_MODEL=` | Model | Use it when |
|---|---|---|
| `offline` (default) | EmbeddingGemma, on this computer | The computer can run it (downloads ~1.5 GB once; search works without internet) |
| `online` | Gemini Embedding 2, with your `GEMINI_API_KEY` | The computer can't run the local model (older or low-memory machines). Nothing large is downloaded |

Restart the app after changing it; Settings shows which one is active. The two models' results can't be mixed, so
each source keeps a separate index per model. Sources indexed with the other one are updated automatically in the
background ("Updating search" in Sources), from the text already stored: no re-reading of the PDF, and your reading
position stays. Until a source is updated, searching it still works by keywords. Switching back later is instant, since both
indexes are kept. On the free tier, online indexing runs at about 100 chunks a minute (a 50-page PDF takes about 2
minutes); the app paces itself and waits when Google asks it to.

To stop the app, close both windows (or press Ctrl+C in them). Drag a PDF onto `run.bat` to open it
straight in a new notebook. Use Chrome or Edge for the microphone.

Pages have real addresses: `http://127.0.0.1:8765/` (your notebooks) and
`http://127.0.0.1:8765/notebooks/<id>` (one notebook), so the browser's Back/Forward buttons, bookmarks
and new tabs all work.

### Web search and TinyFish

The tutor checks the web when your sources don't cover something, or when you ask it to ("search the internet
for…"), and always says which answer came from your notes (with the page) and which from the web (with the site).
Pick the service with `WEB_SEARCH` in `.env`:

| `WEB_SEARCH=` | What happens | Needs |
|---|---|---|
| `tinyfish` | **TinyFish Search** finds pages (general web, news or research papers, India-targeted when it matters); **TinyFish Fetch** then reads the best few in a real browser, and the tutor answers from the most relevant paragraphs of those pages, not one-line snippets. Also lets you add any **web page as a source** (below) | `TINYFISH_API_KEY` ([get one](https://agent.tinyfish.ai/api-keys); Search and Fetch have a free daily allowance) |
| `tavily` | Tavily search snippets | `TAVILY_API_KEY` |
| `duckduckgo` | DuckDuckGo search snippets | nothing |
| *(empty)* | Tavily if `TAVILY_API_KEY` is set, otherwise DuckDuckGo (the behaviour before this setting existed) | |

If the chosen service fails (no internet, a daily limit), the next available one answers (TinyFish, then Tavily, then
DuckDuckGo), and the tutor is told which one did. Settings › System shows the active service, and the conversation
shows "Searched the web with TinyFish for …" with a chip for every site used.

**How TinyFish is used, step by step**
1. The voice model (Gemini Live) decides it needs the web and calls the app's `web_search` tool. It writes the
   search as English keywords (even when you spoke Hindi), passes your question in your words, and picks the kind
   (general, news, research papers), a time window for anything current, and whether it's about India.
2. `tutor/web.py` sends that to **TinyFish Search** (`GET api.search.tinyfish.ai`): `language=en`, `location=IN` for
   Indian topics, `domain_type` news or research_paper, a recency window (or publication years for papers), a
   `purpose` that includes your question, and `exclude_domains` for social, login-walled and homework-dump sites
   (Quora, Pinterest, Chegg, Course Hero, Scribd, Reddit, ...).
3. The results are re-ranked with a small boost for reliable sources (universities, `.gov`/`.ac.in`/`.nic.in`,
   Wikipedia, NCERT, NPTEL, Britannica, ...), one per site, duplicates dropped.
4. The best pages that can be read go to **TinyFish Fetch** (`POST api.fetch.tinyfish.ai`, Markdown) at the same
   time, one request each, so one slow site can't hold up the rest. Videos and sites that block automated reading
   are skipped (still listed as sources), and a page that comes back nearly empty is passed over for the next. The
   whole search stays within about 9 seconds (typically 6 to 7), measured from India.
5. From each page it keeps the paragraphs that best match your question (rare words count more), with their nearest
   heading. Menus are dropped, and so is any text that tries to give the model instructions.
6. The tutor answers from that text, naming each source the way you'd say it ("according to Wikipedia", "The Hindu
   reports"), giving dates for anything current, and comparing it with your notes, which win for your course.
   Asking the same thing again within 10 minutes is answered instantly from the last search.

**Add from a link** (the link button in Sources, or "Add from a link" on an empty notebook): paste an address.
- A link straight to a PDF file is downloaded and added as it is (works with any `WEB_SEARCH`).
- A web page is read with **TinyFish Fetch** as clean HTML (menus, scripts and images dropped) and laid out as a PDF
  whose outline is the page's headings (`tutor/web_source.py`). From there it's an ordinary source: outline, search,
  page viewer, page citations, teaching in order. Its first line names the original address.
- The server only downloads from public addresses (not this computer or a private network), checked again on
  every redirect.

### Hosting it online (optional)

The same code can also run as a website you sign in to from anywhere: the interface on Vercel and the Python API
on your own server (an Oracle Cloud VM, for example), with one email + password account, HTTPS, and online
embeddings. Everything for that is in [`deploy/`](deploy/README.md); nothing about the local setup changes.

## Logs

| File | What |
|---|---|
| `logs\voice-tutor.log` | Everything the API server does (rotates at 5 MB): requests, uploads and indexing stages, voice sessions (connect time, tool calls with timings, reply latency, each exchange), errors with tracebacks |
| `logs\errors.log` | API warnings and errors only — check this first when something goes wrong |
| `logs\ui.log` | The UI server (Next.js) output |
| `logs\embedder-crash.log` | The embedding model's process: the computer's details at each start, and the stack trace if it ever crashes |
| `logs\crash.log` | Stack trace if the API server itself ever crashes outside Python |
| `logs\setup.log` | run.bat steps, pip and npm output, UI builds |

If indexing a PDF fails on a computer, the source shows the reason (for example an older processor or
too little free memory) and the app keeps running. If the local model can't run on that computer at all, set
`EMBEDDING_MODEL=online` in `.env` (see above). The embedding model runs in its own process: when it
crashes, the app restarts it in a slower safe mode and carries on. Send `logs\embedder-crash.log` and
`logs\voice-tutor.log` if you need help.

Each window shows its own events live (polling requests are file-only to keep it readable).
Run `python app.py --debug` to see everything in the API window too.

## What's in the app

- **Your library**: notebooks appear as books on a shelf, each in its own cloth colour (a new one gets the colour
  used least; change it from the book's menu). "Continue reading" opens the book you're in at the page you stopped.
  Long-press a book on a phone (or right-click) for its menu.
- **Notebooks** with multiple PDF sources each; every PDF has a colour of its own, shown as a small spine. Search covers all sources; teaching follows the active
  source. Each PDF remembers where you stopped, so "continue" picks up from there.
- **Voice mode**: live, interruptible conversation (Gemini Live). The text bar works like ChatGPT's: type, or
  tap the orb on its right to talk. While talking, the bar has the mic, a box to keep typing, and **End**; the
  conversation stays on screen with a floating orb above the bar. Tap that orb for the full orb view, and
  tap the big orb to go back to the transcript (your choice is remembered). The voice orb flows and swells with the
  tutor's voice; captions and spoken page citations. Typing works too and gets spoken answers.
- **Voice previews**: press play on any voice in Settings to hear it before choosing (generated once, cached).
- **Tutor manner** (Settings > Tutor): how encouraging it is (Minimal, Balanced, Warm), how much emotion is in
  its voice (Calm, Natural, Lively) and how it teaches (Direct, Step by step, Socratic, Exam prep). Applies from
  the next conversation; the wording sent to the model is in `tutor/voice/style.py`.
- **PDF viewer** that scrolls through the whole document vertically. Click any citation chip to jump to
  it; zoom with the buttons or Ctrl+scroll.
- **Resizable panels**: drag the thin handles between Sources, Conversation and the PDF (double-click
  to reset; drag Sources all the way left to hide it). Sizes are remembered.
- **Full-screen reading** (the ⛶ button in the viewer, or press F): the PDF fills the screen and the
  tutor becomes a floating orb with a speech bubble showing what it says. Tap the orb to talk; Esc exits.
- **Outline**: every section with its pages; the one you're on is marked. Tap a section to have it taught.
- **Uploads** show progress the whole way: the upload itself (with a percentage), reading the PDF (with a timer),
  indexing, and the new source appears the moment it's ready.
- **Phones**: the layout switches to Sources / Chat / Page tabs; pinch to zoom the PDF; every control works by
  touch; the screen stays on while the app is open.
- **Web research** with TinyFish (search, then the top pages read in a real browser), Tavily or DuckDuckGo:
  `WEB_SEARCH` in `.env` (see [Web search and TinyFish](#web-search-and-tinyfish)).
- **Add from a link**: a web page (read with TinyFish) or a PDF file on the web becomes a source like any PDF.
- **How the tutor teaches** (`prompts/system.md`, built from tutoring research): complete explanations with page
  references, then a question that makes you use what you learned; hints before answers when you're wrong; it
  doesn't give in when you insist on something your notes contradict; it won't write work you'll submit but guides
  you to it; it keeps chit-chat and unrelated requests short; safety help (Tele-MANAS 14416, 112) when it matters.

## Where things live

| What | Where |
|---|---|
| Code, `.env`, `logs\` | this folder |
| Python backend (API, voice, retrieval) | `app.py`, `tutor\` |
| User interface (Next.js, React, TypeScript) | `ui.py`, `frontend\` (`frontend\src\app` = pages and routes) |
| Python virtual environment | `.venv\` in this folder (created by run.bat) |
| UI packages and build | `frontend\node_modules\`, `frontend\.next\` (created by run.bat) |
| Your notebooks, indexes, uploaded PDFs | `%LOCALAPPDATA%\voice-tutor` |

This folder is inside OneDrive, so OneDrive also syncs `.venv` (~2 GB). That's why the first install is
slow. If an install ever fails with "Access is denied", pause OneDrive syncing and run `run.bat` again.
Moving the project out of OneDrive (e.g. `C:\dev\voice-tutor`) avoids both.

Use the venv manually (PowerShell, in this folder):
```powershell
.\.venv\Scripts\Activate.ps1
python app.py                     # the API server (port 8766)
python ui.py serve                # the UI server (port 8765), after: python ui.py build
python ui.py serve --dev          # UI with hot reload while editing frontend\ (no build needed)
python -m pytest -q               # tests
python ingest.py file.pdf         # index a PDF from the command line
python query.py "question"        # debug retrieval
python console.py                 # text console over one document (debugging)
python manage.py data info        # what's stored; also: data prune, data backup <file>, user ... (hosted login)
```

Recreate the venv from scratch: delete the `.venv` folder and run `run.bat`. The same goes for the UI:
delete `frontend\node_modules` and `frontend\.next`. Ports can be changed in `.env` with
`VOICE_TUTOR_PORT` (UI, default 8765) and `VOICE_TUTOR_API_PORT` (API, default 8766).

## Privacy

PDF parsing, indexing and search run on your computer. The voice model is Gemini Live: what you say and
the passages it reads are sent to Google, and on the free tier Google may use them to improve its
products. With `EMBEDDING_MODEL=online`, your PDFs' text is also sent to Google to be indexed. Web searches go
to the service you chose with `WEB_SEARCH` (the question, and with TinyFish the addresses of the pages it reads);
"Add from a link" sends the address to TinyFish when it's a web page.

Even locally, the API refuses requests that change anything when they come from another website (so a page you
visit can't drive the app behind your back), and the pages carry a strict Content Security Policy.
