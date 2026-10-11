# Voice Tutor

Talk one-to-one with your PDFs, like NotebookLM but as a live voice conversation. One partner that is
your **tutor** (teaches the material in order and tracks progress), **explainer** (answers from your
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

To stop the app, close both windows (or press Ctrl+C in them). Drag a PDF onto `run.bat` to open it
straight in a new notebook. Use Chrome or Edge for the microphone.

Pages have real addresses: `http://127.0.0.1:8765/` (your notebooks) and
`http://127.0.0.1:8765/notebooks/<id>` (one notebook), so the browser's Back/Forward buttons, bookmarks
and new tabs all work.

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
too little free memory) and the app keeps running. The embedding model runs in its own process: when it
crashes, the app restarts it in a slower safe mode and carries on. Send `logs\embedder-crash.log` and
`logs\voice-tutor.log` if you need help.

Each window shows its own events live (polling requests are file-only to keep it readable).
Run `python app.py --debug` to see everything in the API window too.

## What's in the app

- **Notebooks** with multiple PDF sources each. Search covers all sources; teaching follows the active
  source. Each PDF remembers where you stopped and what's covered.
- **Voice mode**: live, interruptible conversation (Gemini Live). The text bar works like ChatGPT's: type, or
  tap the orb on its right to talk. While talking, the bar has the mic, a box to keep typing, and **End**; the
  conversation stays on screen with a floating orb above the bar. Tap that orb for the full orb view, and
  tap the big orb to go back to the transcript (your choice is remembered). The voice orb flows and swells with the
  tutor's voice; captions and spoken page citations. Typing works too and gets spoken answers.
- **Voice previews**: press play on any voice in Settings to hear it before choosing (generated once, cached).
- **PDF viewer** that scrolls through the whole document vertically. Click any citation chip to jump to
  it; zoom with the buttons or Ctrl+scroll.
- **Follow along**: while the tutor talks, the viewer highlights the sentence it's explaining, in time
  with its voice, page after page. It only moves to another passage when the tutor is clearly talking
  about it, and otherwise stays put rather than guessing. Scroll away to read something else and
  following pauses; tap "Back to the tutor" (or just ask your next question) to pick it up again.
- **Resizable panels**: drag the thin handles between Sources, Conversation and the PDF (double-click
  to reset; drag Sources all the way left to hide it). Sizes are remembered.
- **Full-screen reading** (the ⛶ button in the viewer, or press F): the PDF fills the screen and the
  tutor becomes a floating orb with a speech bubble showing what it says. Tap the orb to talk; Esc exits.
- **Outline with progress**: each section shows completed (tick), in progress (tinted) or not started. Click a
  section's title to have it taught; click its circle to mark it completed or not (Shift+click: everything up
  to there); the outline menu can mark everything completed or reset. You can also just ask the tutor ("mark
  everything up to 14 as done", "I already know chapter 2", "unmark 3.1"). Reading with the tutor marks what's
  read automatically; a chapter completes when all of it is done.
- **Web research** via Tavily when `TAVILY_API_KEY` is set in `.env`, otherwise DuckDuckGo (free, no key).

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
```

Recreate the venv from scratch: delete the `.venv` folder and run `run.bat`. The same goes for the UI:
delete `frontend\node_modules` and `frontend\.next`. Ports can be changed in `.env` with
`VOICE_TUTOR_PORT` (UI, default 8765) and `VOICE_TUTOR_API_PORT` (API, default 8766).

## Privacy

PDF parsing, indexing and search run on your computer. The voice model is Gemini Live: what you say and
the passages it reads are sent to Google, and on the free tier Google may use them to improve its
products.
