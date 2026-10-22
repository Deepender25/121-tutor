"""WebSocket bridge: browser <-> VoiceSession (Gemini Live), with tools running locally.

Browser -> server: binary frames = 16 kHz mono PCM16 mic audio;
                   JSON {"type": "text", "text", "display"?} (display: a short version for the history) |
                        {"type": "audio_end"} | {"type": "close"} |
                        {"type": "client_stats", ...} (playback diagnostics, logged to the file)
Server -> browser: binary frames = 24 kHz mono PCM16 speech;
                   JSON {"type": "status"|"transcript"|"interrupted"|"turn_complete"|"tool_call"|
                         "tool_result"|"notice"|"error", ...}
"""

import asyncio
import re
import contextlib
import json
import logging
import time
import wave

import numpy as np

from fastapi import WebSocket, WebSocketDisconnect

from tutor import config
from tutor.intent import wants_web_search
from tutor.logs import short
from tutor.server.state import AppState
from tutor.tools import NotebookTools
from tutor.voice.base import OUTPUT_SAMPLE_RATE, VoiceEvent
from tutor.voice.gemini_live import GeminiLiveSession
from tutor.voice.prompt import build_system_prompt

log = logging.getLogger("live")


WEB_NUDGE = ("(Message from the app, not the person: their last message asked you to search the internet, and you "
             "answered without calling web_search. Call web_search now with a good query, then answer from those "
             "results and name the websites. If they actually told you not to search, just say \"Okay\" and wait. "
             "Don't apologise at length; just say you're searching now.)")
CONTINUE_NUDGE = ("(Message from the app, not the person: you said what you'd do next but ended your turn without "
                  "doing it. Do it now: call the tool and teach, without repeating what you already said.)")
FINISH_SECTION_NUDGE = ("(Message from the app, not the person: you stopped in the middle of a section; {left} of it "
                        "still to come, and they didn't ask you to stop. Carry on: call read_passage with target next "
                        "and teach it, without repeating what you already said.)")
SEARCH_LOADING = "Getting search ready. The first start takes about half a minute; the voice starts by itself."
NO_KEY = ("No Gemini API key. Add GEMINI_API_KEY=… to the .env file in the project folder, then close the "
          "\"Voice Tutor - API\" window and run run.bat again.")


def join_text(previous: str, piece: str) -> str:
    """Append a transcript piece, adding the space that's missing when speech resumes after a tool call
    ("…for you.The history…")."""
    if previous and piece and not previous[-1].isspace() and not piece[0].isspace() and previous[-1] in ".!?,;:":
        return previous + " " + piece
    return previous + piece


class MicMeter:
    """The mic level, from the audio the browser sends, so the log can say why a reply was slow: Gemini answers once
    it decides you've finished, and this shows how long after you actually went quiet that was, and how loud the
    room was meanwhile (noise can keep it waiting; a quiet room and a long wait points at the connection)."""

    SPEECH_DB = 12  # this far above the room's level counts as talking
    FLOOR_RISE_DB_S = 2.0  # the room level follows a louder room slowly, so speech doesn't become "the room"
    STUCK_S = 5.0  # quiet this long after talking, with no reply: worth a line in the log
    SILENT_DB = -90.0  # below this the mic is sending zeros, not sound

    def __init__(self):
        self.floor: float | None = None  # room level, dBFS
        self.voice_at: float | None = None  # when the last chunk that sounded like talking arrived
        self.spoke = False  # talked since the last reply started
        self.warned = False
        self.tutor_until = 0.0  # when the tutor's reply audio sent so far ends playing: the mic then is mostly echo
        self._last = time.monotonic()

    def tutor_audio(self, nbytes: int, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.tutor_until = max(self.tutor_until, now) + nbytes / (OUTPUT_SAMPLE_RATE * 2)

    def tutor_stopped(self, now: float | None = None) -> None:
        self.tutor_until = time.monotonic() if now is None else now

    def chunk(self, pcm: bytes, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32)
        if not x.size:
            return
        db = 20 * np.log10(np.sqrt(np.mean(x * x)) / 32768 + 1e-9)
        dt, self._last = now - self._last, now
        if now < self.tutor_until + 0.5:  # the tutor is talking: what the mic hears is mostly its voice
            return
        if db < self.SILENT_DB:  # digital silence (a muted or starting mic), not the room
            return
        if self.floor is None or db < self.floor:
            self.floor = db
        else:
            self.floor = min(db, self.floor + self.FLOOR_RISE_DB_S * max(dt, 0.0))
        if db > self.floor + self.SPEECH_DB:
            self.voice_at = now
            self.spoke = True
            self.warned = False

    def quiet_for(self, now: float | None = None) -> float | None:
        return None if self.voice_at is None else (time.monotonic() if now is None else now) - self.voice_at

    def stuck(self, now: float | None = None) -> bool:
        """True once per turn: they talked, went quiet STUCK_S ago, and no reply has started."""
        quiet = self.quiet_for(now)
        if self.spoke and not self.warned and quiet is not None and quiet >= self.STUCK_S:
            self.warned = True
            return True
        return False

    def taken(self) -> None:
        """Gemini took the turn without speaking yet (a tool call): a slow tool isn't a stuck turn."""
        self.spoke = False

    def replied(self, now: float | None = None) -> str:
        """Called when a reply starts: a note for the log about the turn that led to it ('' if they didn't talk)."""
        quiet = self.quiet_for(now)
        note = (f" (you went quiet {quiet:.1f}s before; room {self.floor:.0f} dBFS)"
                if self.spoke and quiet is not None and self.floor is not None else "")
        self.spoke = False
        return note


_NEXT_STEP = re.compile(
    r"\b(?:let'?s|let us|let me|i'?ll|i will|we'?ll|we will)\b(?!\s+know)[^.?!]*?\b(?:dive|start|begin|move|go|look|"
    r"turn|jump|read|open|continue|explore|get into|head|check|find|search)\b[^.?!]*[.!]?\s*$", re.I)
_WAITING = re.compile(r"\b(?:when(?:ever)? you'?re ready|if you(?:'d)? (?:like|want)|whenever you want|shall we|"
                      r"would you like|do you want)\b", re.I)


# Parts with nothing to teach line by line: a sentence on what they list is enough.
_FRONT_MATTER = re.compile(r"\b(?:contents|syllabus|index|preface|foreword|how to use|legend|acknowledg\w*)\b", re.I)


def announces_next_step(text: str) -> bool:
    """Does what was said end with a sentence announcing a step ("Let's dive into the first topic.", "Let me read that
    section.") rather than a question or an offer that waits for them?"""
    text = " ".join(text.split())
    if not text or text.endswith("?"):
        return False
    last = re.split(r"(?<=[.!?])\s+", text)[-1]
    return bool(_NEXT_STEP.search(last)) and not _WAITING.search(last)


class TeachingGuard:
    """Holds back reading the next part while the part just read hasn't been taught. Once the tutor read five parts in
    a row saying only "let's continue" between them, then asked a question about what it never explained."""

    MIN_CHARS = 200  # spoken since the read; a filler line ("Continuing on, let's look at…") is under 100
    MAX_HOLDS = 2  # after this many in a row, let it through rather than stall the lesson
    MAX_NUDGES = 4  # per thing they said: a section's worth of parts, not an endless loop
    SHORT_PART = 400  # a cover page or a heading on its own: nothing much to teach

    def __init__(self):
        self.pending: dict | None = None  # the part read and not yet taught
        self.spoken = 0
        self.holds = 0
        self.since_tool = ""  # said since the last tool call
        self.nudged = False  # since the last part read (or the last thing they said)
        self.nudges = 0  # since the last thing they said

    def user_input(self) -> None:
        """They said something: "skip", "next" and the like are theirs to decide."""
        self.pending, self.holds = None, 0
        self.nudged, self.nudges = False, 0

    def model_output(self, text: str) -> None:
        self.spoken += len(text.strip())
        self.since_tool += text

    def tool_called(self) -> None:
        self.since_tool = ""

    def needs_nudge(self) -> str | None:
        """At the end of a turn, the message that gets the lesson going again, or None. The tutor once ended a turn
        with "Let's dive right into the first topic." and then silence; and once stopped after the first part of a
        section, with two parts to go, without a word."""
        if self.nudged or self.nudges >= self.MAX_NUDGES:
            return None
        text = " ".join(self.since_tool.split())
        if announces_next_step(text):
            message = CONTINUE_NUDGE
        elif (self.pending and self.pending["parts_left"] and self.spoken >= self.pending["needed"]
              and not text.endswith("?")):
            left = self.pending["parts_left"]
            message = FINISH_SECTION_NUDGE.format(left=f"{left} part{'s' if left != 1 else ''}")
        else:
            return None
        self.nudged = True
        self.nudges += 1
        return message

    def read(self, result: dict) -> None:
        text = result.get("text", "")
        short = len(text) < self.SHORT_PART or bool(_FRONT_MATTER.search(result.get("section", "")))
        self.pending = {"section": result.get("section", ""), "pages": result.get("pages", ""),
                        "needed": 0 if short else min(self.MIN_CHARS, len(text) // 5), "short": short,
                        "parts_left": 0 if result.get("is_section_end") else int(result.get("parts_left_in_section") or 0)}
        self.spoken = 0
        self.holds = 0
        self.nudged = False

    def hold(self, name: str, args: dict | None) -> dict | None:
        """The result to send instead of reading on, or None to go ahead. Only within one reply: anything they say
        in between (skip, go to chapter 3) clears it."""
        if name != "read_passage" or self.pending is None or self.holds >= self.MAX_HOLDS:
            self.holds = 0
            return None
        target = str((args or {}).get("target") or "next").strip().lower()
        p = self.pending
        if target in ("current", "again", "repeat", "previous", "prev", "back"):
            return None
        # Reading anything else before teaching the part just read: it once read three one-part sections (the three
        # principles) back to back, saying only "Let's look at the next principle" between them.
        if self.spoken < p["needed"]:
            self.holds += 1
            return {"not_read_yet": (
                f"You haven't taught the part you just read yet ({p['section']}, page {p['pages']}). Explain every "
                f"point in it out loud now, in your own words; then read the next part.")}
        if target in ("next", "continue") or p["short"] or not p["parts_left"]:
            return None
        # Jumping away mid-section on its own skips parts nobody asked to skip (it once went from a section's first
        # part straight to the next section, leaving out the analogy and the memory trick).
        self.holds += 1
        return {"not_read_yet": (
            f"{p['section']} has {p['parts_left']} more part{'s' if p['parts_left'] != 1 else ''} after this one, "
            f"and they haven't asked to skip it. Call read_passage with target next and teach "
            f"{'them' if p['parts_left'] != 1 else 'it'} first.")}


class WebSearchGuard:
    """Tracks one user request: did they ask for an internet search, and did the model actually search?"""

    def __init__(self):
        self.text, self.searched, self.nudged, self.model_spoke = "", False, False, False

    def user_input(self, text: str) -> None:
        if self.model_spoke:  # a new request starts after the model has answered
            self.text, self.searched, self.nudged, self.model_spoke = "", False, False, False
        self.text += " " + text

    def model_output(self) -> None:
        self.model_spoke = True

    def tool_called(self, name: str) -> None:
        if name == "web_search":
            self.searched = True

    def needs_nudge(self) -> bool:
        if self.nudged or self.searched or not wants_web_search(self.text):
            return False
        self.nudged = True
        return True


class TurnRecorder:
    """Accumulates transcripts into history entries (saved when the model's turn completes)."""

    def __init__(self, state: AppState, notebook_id: str):
        self.state, self.notebook_id = state, notebook_id
        self.user, self.model, self.refs = "", "", []
        self.pending = ""  # typed during a reply: it starts the next exchange once that reply ends
        self.turns = 0

    def user_text(self, text: str) -> None:
        if self.model:  # user barged in after the model spoke: close the previous exchange
            self.flush()
        self.user += text

    def typed(self, text: str) -> None:
        """A typed message. During a reply, Gemini keeps sending that reply's transcript until it reports
        'interrupted', so the message waits and is paired with the answer that follows it."""
        if self.model.strip():
            self.pending += text
        else:
            self.user_text(text)

    def model_text(self, text: str) -> None:
        self.model = join_text(self.model, text)

    def ref(self, ui: dict) -> None:
        if ui and ui.get("kind") in ("search", "passage", "web"):
            self.refs.append(ui)

    def flush(self) -> None:
        entries = []
        if self.user.strip():
            entries.append({"role": "user", "text": " ".join(self.user.split())})
        if self.model.strip():
            entry = {"role": "model", "text": " ".join(self.model.split())}
            if self.refs:
                entry["refs"] = self.refs
            entries.append(entry)
        if entries:
            self.turns += 1
            for e in entries:
                log.info("%s %s", "You:  " if e["role"] == "user" else "Tutor:", short(e["text"], 220))
            with contextlib.suppress(KeyError):  # notebook deleted mid-session
                self.state.notebooks.append_history(self.notebook_id, entries)
        self.user, self.pending = self.pending, ""
        self.model, self.refs = "", []

    def finish(self) -> None:
        """Save everything at the end of a session, including a message typed during the last reply."""
        self.flush()
        self.flush()


class AudioTape:
    """Diagnostics (VOICE_TUTOR_RECORD_AUDIO=on): a session's audio in both directions as WAV files, and a
    timeline that places each event at its position in both recordings, so a sound problem can be traced to
    Gemini's output, the microphone, or playback in the browser."""

    KEEP = 6  # sessions

    def __init__(self, name: str) -> None:
        folder = config.DEBUG_AUDIO_DIR
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.paths = [folder / f"{stamp}-tutor.wav", folder / f"{stamp}-mic.wav", folder / f"{stamp}-timeline.txt"]
        self.tutor_wav = self._wav(self.paths[0], 24000)
        self.mic_wav = self._wav(self.paths[1], 16000)
        self.timeline = open(self.paths[2], "w", encoding="utf-8")  # noqa: SIM115 - closed in close()
        self.t0 = time.perf_counter()
        self.tutor_s = 0.0
        self.mic_s = 0.0
        self.note(f"session: {name}")
        stamps = sorted({p.name[:15] for p in folder.iterdir() if p.name[:8].isdigit()})
        for old in stamps[:-self.KEEP]:
            for p in folder.glob(f"{old}-*"):
                with contextlib.suppress(OSError):
                    p.unlink()

    @staticmethod
    def _wav(path, rate: int) -> wave.Wave_write:
        w = wave.open(str(path), "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        return w

    def tutor(self, pcm: bytes) -> None:
        self.tutor_wav.writeframes(pcm)
        self.tutor_s += len(pcm) / 48000

    def mic(self, pcm: bytes) -> None:
        self.mic_wav.writeframes(pcm)
        self.mic_s += len(pcm) / 32000

    def note(self, text: str) -> None:
        self.timeline.write(f"{time.perf_counter() - self.t0:8.2f}s  tutor.wav@{self.tutor_s:7.2f}s  "
                            f"mic.wav@{self.mic_s:7.2f}s  {text}\n")
        self.timeline.flush()

    def close(self) -> None:
        for f in (self.tutor_wav, self.mic_wav, self.timeline):
            with contextlib.suppress(Exception):
                f.close()


def typed_message(data: dict) -> tuple[str, str] | None:
    """(text for Gemini, text for the history) from a {"type": "text", "text", "display"?} frame, or None.
    `display` is a short version for the chat, e.g. an outline tap's 'Teach me "Design"'."""
    text = (data.get("text") or "").strip()[:4000] if isinstance(data.get("text"), str) else ""
    if data.get("type") != "text" or not text:
        return None
    display = data.get("display")
    display = display.strip()[:400] if isinstance(display, str) else ""
    return text, display or text


async def wait_for_search(ws: WebSocket, state: AppState) -> list[tuple[str, str]] | None:
    """Wait for the search model (it loads for about half a minute after the app starts), telling the page
    why, and keeping any messages typed meanwhile. Returns those messages, or None if the page left: the
    caller then ends at once, so an abandoned attempt doesn't hold one of the voice-session slots."""
    early: list[tuple[str, str]] = []
    if state.embedder_ready.is_set():
        return early
    await ws.send_text(json.dumps({"type": "notice", "text": SEARCH_LOADING}, ensure_ascii=False))
    log.info("Voice session waiting for the search model to load")
    wait = asyncio.create_task(asyncio.to_thread(state.require_embedder))
    # The thread can't be stopped, and if the page leaves first nobody reads its outcome: retrieve it here to
    # keep "Task exception was never retrieved" out of the log. The caller asks require_embedder again.
    wait.add_done_callback(lambda t: t.cancelled() or t.exception())
    while not wait.done():
        recv = asyncio.create_task(ws.receive())
        await asyncio.wait({wait, recv}, return_when=asyncio.FIRST_COMPLETED)
        if not recv.done():
            recv.cancel()
            with contextlib.suppress(BaseException):
                await recv
            break
        try:
            msg = recv.result()
        except Exception:  # the connection failed outright
            log.info("The page left while search was loading")
            return None
        data = None
        if msg["type"] != "websocket.disconnect" and msg.get("text"):
            with contextlib.suppress(ValueError):
                data = json.loads(msg["text"])
        if msg["type"] == "websocket.disconnect" or (isinstance(data, dict) and data.get("type") == "close"):
            log.info("The page left while search was loading")
            return None
        typed = typed_message(data) if isinstance(data, dict) else None
        if typed:
            early.append(typed)  # mic audio (bytes) and other messages are dropped: the voice isn't on yet
    return early


async def live_endpoint(ws: WebSocket, notebook_id: str, state: AppState) -> None:
    await ws.accept()
    outgoing: asyncio.Queue = asyncio.Queue(maxsize=2000)

    async def sender() -> None:
        while True:
            item = await outgoing.get()
            if item is None:
                return
            if isinstance(item, bytes):
                await ws.send_bytes(item)
            else:
                await ws.send_text(json.dumps(item, ensure_ascii=False))

    async def fail(message: str) -> None:
        log.warning("Voice session refused: %s", message)
        await ws.send_text(json.dumps({"type": "error", "message": message, "fatal": True}))
        await ws.close()

    try:
        notebook = state.notebooks.get(notebook_id)
    except KeyError:
        return await fail("This notebook no longer exists.")
    if not state.has_gemini_key():
        return await fail(NO_KEY)
    early_texts = await wait_for_search(ws, state)
    if early_texts is None:  # the page left while search was loading
        with contextlib.suppress(Exception):
            await ws.close()
        return
    try:
        embedder = state.require_embedder(timeout=0)  # ready (or failed) by now
    except RuntimeError as e:
        return await fail(str(e))

    recorder = TurnRecorder(state, notebook_id)
    guard = WebSearchGuard()
    teaching = TeachingGuard()
    started = time.perf_counter()
    voice = state.settings()["voice"]
    style = state.settings()["style"]
    log.info("Voice session started: \"%s\" (%d source%s, voice %s, model %s, tutor %s)", notebook.name,
             len(notebook.sources), "" if len(notebook.sources) == 1 else "s", voice, config.LIVE_MODEL,
             "/".join(style.values()))
    timing = {"input_at": None}  # when the user's latest input arrived, for reply-latency logging
    meter = MicMeter()
    interruptions = [0]  # replies cut off by (what Gemini heard as) the person talking: echo shows up here
    tape = AudioTape(notebook.name) if config.RECORD_AUDIO else None
    if tape:
        log.info("Recording this session's audio for diagnostics: %s", tape.paths[0].parent)

    def on_active_change(doc_id: str) -> None:
        with contextlib.suppress(KeyError):
            state.notebooks.update(notebook_id, active_source=doc_id)

    tools = NotebookTools(state.cache, embedder, notebook.sources, notebook.active_source, on_active_change)
    background: set[asyncio.Task] = set()  # fire-and-forget tasks, referenced until they finish

    async def emit(event: VoiceEvent) -> None:
        if event.kind == "audio":
            if timing["input_at"] is not None:
                log.info("Reply audio started %.2fs after your input%s", time.perf_counter() - timing["input_at"],
                         meter.replied())
                timing["input_at"] = None
            else:
                meter.replied()
            if outgoing.full():  # browser not keeping up: drop audio rather than block the session
                log.warning("Dropped %d bytes of reply audio: the browser isn't keeping up", len(event.audio))
                if tape:
                    tape.note(f"DROPPED {len(event.audio)} bytes")
                return
            if tape:
                tape.tutor(event.audio)
            meter.tutor_audio(len(event.audio))
            outgoing.put_nowait(event.audio)
            return
        if tape and event.kind in ("transcript", "interrupted", "turn_complete", "tool_call", "error"):
            tape.note(f"{event.kind} {short(json.dumps(event.data, ensure_ascii=False), 200)}")
        if event.kind == "transcript":
            if event.data["role"] == "user":
                recorder.user_text(event.data["text"])
                guard.user_input(event.data["text"])
                teaching.user_input()
                timing["input_at"] = time.perf_counter()
            else:
                recorder.model_text(event.data["text"])
                teaching.model_output(event.data["text"])
                guard.model_output()
        elif event.kind == "interrupted":
            interruptions[0] += 1
            meter.tutor_stopped()
            log.info("Reply interrupted (Gemini heard speech)")
            if recorder.pending:  # the old reply is over: what was typed during it starts the next exchange
                recorder.flush()
        elif event.kind == "tool_call":
            guard.tool_called(event.data.get("name", ""))
            teaching.tool_called()
            meter.taken()
        elif event.kind == "tool_result":
            recorder.ref(event.data.get("ui"))
        elif event.kind == "turn_complete":
            recorder.flush()
            guard.model_output()
            if guard.needs_nudge():
                # Safety net: the person asked for an internet search and the model answered without one.
                log.warning("Tutor answered an internet-search request without searching; asking it to search now")
                nudge = asyncio.create_task(session.send_text(WEB_NUDGE))
                background.add(nudge)
                nudge.add_done_callback(background.discard)
            elif carry_on := teaching.needs_nudge():
                # Safety net: the turn ended announcing a step it didn't take, or in the middle of a section.
                log.warning("Tutor stopped mid-lesson (%s); asking it to carry on",
                            "announced a step without taking it" if carry_on == CONTINUE_NUDGE else "mid-section")
                nudge = asyncio.create_task(session.send_text(carry_on))
                background.add(nudge)
                nudge.add_done_callback(background.discard)
        elif event.kind == "error":
            log.warning("Voice error: %s", event.data.get("message"))
        elif event.kind == "status" and event.data.get("state") in ("reconnecting", "closed"):
            log.info("Gemini Live: %s", event.data.get("state"))
        await outgoing.put({"type": event.kind, **event.data})

    async def run_tool(name: str, args: dict):
        with contextlib.suppress(KeyError):  # sources may have been added/removed/switched mid-session
            current = state.notebooks.get(notebook_id)
            tools.doc_ids = list(current.sources)
            if current.active_source in tools.doc_ids:
                tools.active = current.active_source
            elif tools.active not in tools.doc_ids:
                tools.active = tools.doc_ids[0] if tools.doc_ids else None
        t0 = time.perf_counter()
        held = teaching.hold(name, args)
        if held is not None:
            log.info("Held back %s(%s): the part just read hasn't been taught yet", name,
                     short(json.dumps(args, ensure_ascii=False), 120))
            return held, None
        outcome = await asyncio.to_thread(tools.call, name, args)
        if name == "read_passage" and "error" not in outcome.result:
            teaching.read(outcome.result)
        ms = (time.perf_counter() - t0) * 1000
        result = outcome.result
        if "error" in result:
            log.warning("Tool %s(%s) -> error: %s (%.0f ms)", name, short(json.dumps(args, ensure_ascii=False), 120),
                        result["error"], ms)
        else:
            detail = (f"{len(result['results'])} results" if "results" in result
                      else f"{result.get('section', '')} p.{result.get('pages', '')}" if name == "read_passage" else "ok")
            log.info("Tool %s(%s) -> %s (%.0f ms)", name, short(json.dumps(args, ensure_ascii=False), 120), detail, ms)
        return outcome.result, outcome.ui

    def build_prompt() -> str:
        # Rebuilt on every reconnect (fresh reading position and history), but with the manner chosen when the
        # conversation started: Settings changes apply from the next conversation, like the voice.
        return build_system_prompt(state.notebooks.get(notebook_id), state.cache, style)

    session = GeminiLiveSession(
        api_key=config.gemini_api_key(),
        model=config.LIVE_MODEL,
        voice=voice,
        build_prompt=build_prompt,
        run_tool=run_tool,
        emit=emit,
        idle_timeout=config.LIVE_IDLE_TIMEOUT_S,
    )
    send_task = asyncio.create_task(sender())
    run_task = asyncio.create_task(session.run())

    async def send_typed(text: str, display: str) -> None:
        recorder.typed(display)
        guard.user_input(text)
        teaching.user_input()
        timing["input_at"] = time.perf_counter()
        await session.send_text(text)  # queued by the session until it's connected

    for text, display in early_texts:
        await send_typed(text, display)

    async def handle(msg: dict) -> bool:
        """One message from the browser. Returns False when the page ended the session."""
        if msg.get("bytes") is not None:
            if tape:
                tape.mic(msg["bytes"])
            meter.chunk(msg["bytes"])
            if meter.stuck():
                log.warning("No reply yet: you went quiet %.0fs ago (room %.0f dBFS). Gemini answers when it decides "
                            "you've finished; a quiet room and no answer points at the connection", meter.quiet_for(),
                            meter.floor)
            await session.send_audio(msg["bytes"])
            return True
        try:
            data = json.loads(msg.get("text") or "{}")
        except ValueError:
            log.debug("Skipped a message from the browser that isn't JSON: %s", short(msg.get("text") or "", 120))
            return True
        if not isinstance(data, dict):
            log.debug("Skipped a message from the browser that isn't an object")
            return True
        kind = data.get("type")
        if kind == "text":
            typed = typed_message(data)
            if typed:
                await send_typed(*typed)
        elif kind == "audio_end":
            await session.end_audio()
        elif kind == "client_stats":
            stats = {k: v for k, v in data.items() if k != "type"}
            # Debug level: in the log file, not on the API window's console.
            log.debug("Playback in the browser: %s", short(json.dumps(stats, ensure_ascii=False), 1500))
            if tape:
                tape.note(f"client_stats {json.dumps(stats, ensure_ascii=False)}")
        elif kind == "close":
            log.info("The page ended the session")
            return False
        return True

    async def reader() -> None:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                # 1000/1001: the page closed it; 1006: the connection dropped; 1011: no reply to keep-alive pings
                # (the browser stopped reading: a suspended or frozen page, or the network)
                log.info("Browser connection closed (code %s)", msg.get("code"))
                if tape:
                    tape.note(f"browser connection closed (code {msg.get('code')})")
                return
            try:
                if not await handle(msg):
                    return
            except Exception:  # one bad message must not end the conversation
                log.exception("Couldn't handle a message from the browser")

    read_task = asyncio.create_task(reader())
    try:
        # Ends when the browser leaves, or the session ends (idle timeout / fatal error).
        done, _ = await asyncio.wait({read_task, run_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                log.error("The voice session stopped because of an error", exc_info=task.exception())
    except WebSocketDisconnect:
        pass
    finally:
        await session.close()
        recorder.finish()
        if tape:
            tape.note("session ended")
            tape.close()
        minutes, seconds = divmod(int(time.perf_counter() - started), 60)
        log.info("Voice session ended after %dm%02ds, %d exchange%s, %d interruption%s", minutes, seconds,
                 recorder.turns, "" if recorder.turns == 1 else "s", interruptions[0], "" if interruptions[0] == 1 else "s")
        for task in (read_task, run_task):
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.gather(read_task, run_task, return_exceptions=True), 5)
        await outgoing.put(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(send_task, 2)
        send_task.cancel()
        with contextlib.suppress(Exception):
            await ws.close()
