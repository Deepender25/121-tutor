# Voice Tutor

Talk one-to-one with your PDFs, like NotebookLM but as a live voice conversation. One partner that is
your **tutor** (teaches the material in order and tracks progress), **explainer** (answers from your
sources with page references), **researcher** (checks the web and says which is which) and
**brainstormer**. Design and decisions: [DESIGN.md](DESIGN.md).

## Quick start (Windows)

1. Double-click **`run.bat`**. It runs four steps in a small setup window:
   1. **Virtual environment**: creates `.venv\` in this folder (first run only) and checks it's valid.
   2. **Dependencies**: installs everything into `.venv` (first run ~1–2 GB, a few minutes; again only
      when `requirements.lock` changes or packages are missing). Full pip output: `logs\setup.log`.
   3. **.env**: created from `.env.example` if missing; warns if the Gemini key is empty.
   4. **Server**: starts in its own terminal window, **"Voice Tutor - Server"**, with live logs, and opens
      the app in your browser when it's ready (http://127.0.0.1:8765). If it's already running, it just
      opens the browser.
2. Put your free Gemini key in `.env` after `GEMINI_API_KEY=` (https://aistudio.google.com/apikey) and
   restart.
3. Create a notebook, drop in PDFs, tap the orb and talk. Or type.

To stop the app, close the "Voice Tutor - Server" window (or press Ctrl+C in it). Drag a PDF onto
`run.bat` to open it straight in a new notebook. Use Chrome or Edge for the microphone.

## Logs

| File | What |
|---|---|
| `logs\voice-tutor.log` | Everything the server does (rotates at 5 MB): requests, uploads and indexing stages, voice sessions (connect time, tool calls with timings, reply latency, each exchange), errors with tracebacks |
| `logs\errors.log` | Warnings and errors only — check this first when something goes wrong |
| `logs\setup.log` | run.bat steps and full pip install output |

The server window shows the same events live (polling requests are file-only to keep it readable).
Run `python app.py --debug` to see everything in the window too.

## What's in the app

- **Notebooks** with multiple PDF sources each. Search covers all sources; teaching follows the active
  source. Each PDF remembers where you stopped and what's covered.
- **Voice mode**: live, interruptible conversation (Gemini Live). The voice orb flows and swells with the
  tutor's voice; captions and spoken page citations. Typing works too and gets spoken answers.
- **Voice previews**: press play on any voice in Settings to hear it before choosing (generated once, cached).
- **PDF viewer** that scrolls through the whole document vertically. Click any citation chip to jump to
  it; zoom with the buttons or Ctrl+scroll.
- **Follow along**: while the tutor talks, the viewer highlights the sentence it's explaining, in time
  with its voice, page after page. Scroll away to read something else and following pauses; tap
  "Back to the tutor" (or just ask your next question) to pick it up again.
- **Resizable panels**: drag the thin handles between Sources, Conversation and the PDF (double-click
  to reset; drag Sources all the way left to hide it). Sizes are remembered.
- **Full-screen reading** (the ⛶ button in the viewer, or press F): the PDF fills the screen and the
  tutor becomes a floating orb with a speech bubble showing what it says. Tap the orb to talk; Esc exits.
- **Outline** with progress ticks: click a section to have it taught.
- **Web research** via Tavily when `TAVILY_API_KEY` is set in `.env`, otherwise DuckDuckGo (free, no key).

## Where things live

| What | Where |
|---|---|
| Code, `.env`, `logs\` | this folder |
| Python virtual environment | `.venv\` in this folder (created by run.bat) |
| Your notebooks, indexes, uploaded PDFs | `%LOCALAPPDATA%\voice-tutor` |

This folder is inside OneDrive, so OneDrive also syncs `.venv` (~2 GB). That's why the first install is
slow. If an install ever fails with "Access is denied", pause OneDrive syncing and run `run.bat` again.
Moving the project out of OneDrive (e.g. `C:\dev\voice-tutor`) avoids both.

Use the venv manually (PowerShell, in this folder):
```powershell
.\.venv\Scripts\Activate.ps1
python app.py                     # the server (same as run.bat step 4)
python -m pytest -q               # tests
python ingest.py file.pdf         # index a PDF from the command line
python query.py "question"        # debug retrieval
python console.py                 # text console over one document (debugging)
```

Recreate the venv from scratch: delete the `.venv` folder and run `run.bat`.

## Privacy

PDF parsing, indexing and search run on your computer. The voice model is Gemini Live: what you say and
the passages it reads are sent to Google, and on the free tier Google may use them to improve its
products.
