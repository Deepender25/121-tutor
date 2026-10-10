"""WebSocket bridge: browser <-> VoiceSession (Gemini Live), with tools running locally.

Browser -> server: binary frames = 16 kHz mono PCM16 mic audio;
                   JSON {"type": "text", "text"} | {"type": "audio_end"} | {"type": "close"}
Server -> browser: binary frames = 24 kHz mono PCM16 speech;
                   JSON {"type": "status"|"transcript"|"interrupted"|"turn_complete"|"tool_call"|
                         "tool_result"|"error", ...}
"""

import asyncio
import contextlib
import json
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect

from tutor import config
from tutor.intent import wants_web_search
from tutor.logs import short
from tutor.server.state import AppState
from tutor.tools import NotebookTools
from tutor.voice.base import VoiceEvent
from tutor.voice.gemini_live import GeminiLiveSession
from tutor.voice.prompt import build_system_prompt

log = logging.getLogger("live")


WEB_NUDGE = ("(Message from the app, not the person: they asked you to search the internet, but you answered "
             "without calling web_search. Call web_search now with a good query, then answer from those results "
             "and name the websites. Don't apologise at length; just say you're searching now.)")


def join_text(previous: str, piece: str) -> str:
    """Append a transcript piece, adding the space that's missing when speech resumes after a tool call
    ("…for you.The history…")."""
    if previous and piece and not previous[-1].isspace() and not piece[0].isspace() and previous[-1] in ".!?,;:":
        return previous + " " + piece
    return previous + piece


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


def without_text(ui: dict) -> dict:
    """The full chunk text is only for live follow-along; history keeps the citation, not the passage."""
    ui = {k: v for k, v in ui.items() if k != "text"}
    if "refs" in ui:
        ui["refs"] = [{k: v for k, v in r.items() if k != "text"} for r in ui["refs"]]
    return ui


class TurnRecorder:
    """Accumulates transcripts into history entries (saved when the model's turn completes)."""

    def __init__(self, state: AppState, notebook_id: str):
        self.state, self.notebook_id = state, notebook_id
        self.user, self.model, self.refs = "", "", []
        self.turns = 0

    def user_text(self, text: str) -> None:
        if self.model:  # user barged in after the model spoke: close the previous exchange
            self.flush()
        self.user += text

    def model_text(self, text: str) -> None:
        self.model = join_text(self.model, text)

    def ref(self, ui: dict) -> None:
        if ui and ui.get("kind") in ("search", "passage", "web"):
            self.refs.append(without_text(ui))

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
        self.user, self.model, self.refs = "", "", []


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
        return await fail("No Gemini API key. Add GEMINI_API_KEY to the .env file in the project folder and "
                          "restart the app.")
    try:
        embedder = await asyncio.to_thread(state.require_embedder)
    except RuntimeError as e:
        return await fail(str(e))

    recorder = TurnRecorder(state, notebook_id)
    guard = WebSearchGuard()
    started = time.perf_counter()
    voice = state.settings()["voice"]
    log.info("Voice session started: \"%s\" (%d source%s, voice %s, model %s)", notebook.name, len(notebook.sources),
             "" if len(notebook.sources) == 1 else "s", voice, config.LIVE_MODEL)
    timing = {"input_at": None}  # when the user's latest input arrived, for reply-latency logging

    def on_active_change(doc_id: str) -> None:
        with contextlib.suppress(KeyError):
            state.notebooks.update(notebook_id, active_source=doc_id)

    tools = NotebookTools(state.cache, embedder, notebook.sources, notebook.active_source, on_active_change)

    async def emit(event: VoiceEvent) -> None:
        if event.kind == "audio":
            if timing["input_at"] is not None:
                log.info("Reply audio started %.2fs after your input", time.perf_counter() - timing["input_at"])
                timing["input_at"] = None
            if outgoing.full():  # browser not keeping up: drop audio rather than block the session
                return
            outgoing.put_nowait(event.audio)
            return
        if event.kind == "transcript":
            if event.data["role"] == "user":
                recorder.user_text(event.data["text"])
                guard.user_input(event.data["text"])
                timing["input_at"] = time.perf_counter()
            else:
                recorder.model_text(event.data["text"])
                guard.model_output()
        elif event.kind == "tool_call":
            guard.tool_called(event.data.get("name", ""))
        elif event.kind == "tool_result":
            recorder.ref(event.data.get("ui"))
        elif event.kind == "turn_complete":
            recorder.flush()
            guard.model_output()
            if guard.needs_nudge():
                # Safety net: the person asked for an internet search and the model answered without one.
                log.warning("Tutor answered an internet-search request without searching; asking it to search now")
                asyncio.create_task(session.send_text(WEB_NUDGE))
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
        outcome = await asyncio.to_thread(tools.call, name, args)
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
        return build_system_prompt(state.notebooks.get(notebook_id), state.cache)

    session = GeminiLiveSession(
        api_key=config.gemini_api_key(),
        model=config.LIVE_MODEL,
        voice=state.settings()["voice"],
        build_prompt=build_prompt,
        run_tool=run_tool,
        emit=emit,
        idle_timeout=config.LIVE_IDLE_TIMEOUT_S,
    )
    send_task = asyncio.create_task(sender())
    run_task = asyncio.create_task(session.run())

    async def reader() -> None:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                await session.send_audio(msg["bytes"])
                continue
            data = json.loads(msg.get("text") or "{}")
            kind = data.get("type")
            if kind == "text" and data.get("text", "").strip():
                text = data["text"].strip()[:4000]
                recorder.user_text(text)
                guard.user_input(text)
                timing["input_at"] = time.perf_counter()
                await session.send_text(text)
            elif kind == "audio_end":
                await session.end_audio()
            elif kind == "close":
                return

    read_task = asyncio.create_task(reader())
    try:
        # Ends when the browser leaves, or the session ends (idle timeout / fatal error).
        await asyncio.wait({read_task, run_task}, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        await session.close()
        recorder.flush()
        minutes, seconds = divmod(int(time.perf_counter() - started), 60)
        log.info("Voice session ended after %dm%02ds, %d exchange%s", minutes, seconds, recorder.turns,
                 "" if recorder.turns == 1 else "s")
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
