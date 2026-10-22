"""Gemini Live implementation of VoiceSession.

- One Live connection does speech-in, reasoning, speech-out and turn-taking (server-side VAD).
- Our tools are declared BLOCKING: 3.8 Live defaults to non-blocking, which would let the model start
  answering before the document text arrives.
- Context-window compression is on (no session time cap); resumption handles let us reconnect
  transparently on GoAway / dropped connections (~10 min connection lifetime).
- Built-in google_search is NOT used: it has zero free-tier quota (verified 2026-10-10); web search is
  our own `web_search` tool.
"""

import asyncio
import contextlib
import functools
import logging
import time

from google import genai
from google.genai import errors, types

from tutor import config
from tutor.tools import DECLARATIONS
from tutor.voice.base import (
    INPUT_SAMPLE_RATE,
    EmitFn,
    PromptFn,
    RunToolFn,
    VoiceEvent,
    VoiceSession,
)

log = logging.getLogger("voice.gemini")

AUDIO_MIME = f"audio/pcm;rate={INPUT_SAMPLE_RATE}"
MAX_RECONNECTS = 5
# After this many failed attempts while resuming, give up on the resumption handle and start fresh.
RESUME_ATTEMPTS = 2

FATAL, STALE_HANDLE, TRANSIENT = "fatal", "stale_handle", "transient"
# A first connect that fails this many times is reported at once (usually no internet), instead of after the
# full reconnect backoff (about 34 s). Drops in the middle of a session keep the full backoff.
FIRST_CONNECT_ATTEMPTS = 2

PROMPT_FAILED = ("Couldn't prepare the tutor for this notebook. If a source shows as missing, remove it and add the "
                 "PDF again. Details are in logs\\voice-tutor.log.")
UNREACHABLE = "Couldn't reach Gemini. Check your internet connection, then tap the orb to try again."


@functools.lru_cache(maxsize=4)
def _client_for(api_key: str) -> genai.Client:
    """One client per key, shared by sessions: building one takes about half a second, on the event loop."""
    return genai.Client(api_key=api_key)


def classify_error(e: Exception, resuming: bool) -> str:
    """What to do about a Live API error.
    FATAL: retrying can't help (quota, bad key, unknown model) -> report and stop.
    STALE_HANDLE: the resumption handle is no longer valid -> drop it, start a fresh session.
    TRANSIENT: server hiccup / network -> retry with backoff."""
    low = str(e).lower()
    code = getattr(e, "code", None)
    if "quota" in low or "resource_exhausted" in low:
        return FATAL
    if "api key" in low or "api_key" in low or "permission_denied" in low or "permission denied" in low:
        return FATAL
    if "session not found" in low or "expired" in low or ("handle" in low and "invalid" in low):
        return STALE_HANDLE
    if resuming and code == 1008:  # policy violation on a resumed connect: the handle was rejected
        return STALE_HANDLE
    if "model" in low and ("not found" in low or "not supported" in low or "unsupported" in low):
        return FATAL
    return TRANSIENT


def _friendly_error(e: Exception) -> str:
    low = str(e).lower()
    if "quota" in low or "resource_exhausted" in low:
        return ("Gemini free-tier limit reached for now. Wait a minute and try again; daily limits reset at "
                "midnight Pacific time.")
    if "api key" in low or "api_key" in low or "permission" in low:
        return "Gemini rejected the API key. Check GEMINI_API_KEY in your .env file."
    if "model" in low:
        return "The configured Gemini Live model isn't available to this key. Check VOICE_TUTOR_LIVE_MODEL."
    return f"Voice connection error: {str(e)[:200]}"


class GeminiLiveSession(VoiceSession):
    def __init__(
        self,
        api_key: str,
        model: str,
        voice: str,
        build_prompt: PromptFn,
        run_tool: RunToolFn,
        emit: EmitFn,
        idle_timeout: float = 300,
    ):
        self._client = _client_for(api_key)  # shared: never closed here
        self._model = model
        self._voice = voice
        self._build_prompt = build_prompt
        self._run_tool = run_tool
        self._emit = emit
        self._idle_timeout = idle_timeout

        self._session = None
        self._handle: str | None = None
        self._closing = asyncio.Event()
        self._ready = asyncio.Event()
        self._pending_text: list[str] = []
        self._tool_tasks: dict[str, asyncio.Task] = {}
        self._last_activity = time.monotonic()

    # ---- configuration -----------------------------------------------------------------

    def _config(self) -> types.LiveConnectConfig:
        declarations = [
            types.FunctionDeclaration(
                name=d["name"],
                description=d["description"],
                parameters_json_schema=d["parameters"],
                behavior=types.Behavior.BLOCKING,
            )
            for d in DECLARATIONS
        ]
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=self._build_prompt(),
            tools=[types.Tool(function_declarations=declarations)],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self._voice)
                )
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            session_resumption=types.SessionResumptionConfig(handle=self._handle),
            context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
            # Let people pause mid-sentence without the tutor jumping in.
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    start_of_speech_sensitivity=(types.StartSensitivity.START_SENSITIVITY_LOW
                                                 if config.VAD_START_SENSITIVITY == "low"
                                                 else types.StartSensitivity.START_SENSITIVITY_HIGH),
                    end_of_speech_sensitivity=(types.EndSensitivity.END_SENSITIVITY_LOW
                                               if config.VAD_END_SENSITIVITY == "low"
                                               else types.EndSensitivity.END_SENSITIVITY_HIGH),
                    silence_duration_ms=config.VAD_SILENCE_MS,
                    prefix_padding_ms=config.VAD_PREFIX_MS,
                )
            ),
        )

    # ---- lifecycle ---------------------------------------------------------------------

    async def run(self) -> None:
        failures = 0
        first = True
        fresh_restart = False  # True when we had to abandon the old session (show a notice once ready)
        watchdog = asyncio.create_task(self._idle_watchdog())
        try:
            while not self._closing.is_set():
                await self._status("connecting" if first else "reconnecting")
                connect_started = time.monotonic()
                resuming = self._handle is not None
                try:
                    connect_config = self._config()  # builds the system prompt: a bug here won't be fixed by retrying
                except Exception:
                    log.exception("Couldn't build the session config")
                    await self._emit(VoiceEvent("error", {"message": PROMPT_FAILED, "fatal": True}))
                    break
                try:
                    async with self._client.aio.live.connect(model=self._model, config=connect_config) as session:
                        how = (" (resumed the previous session)" if resuming
                               else " (fresh session, recent conversation carried over)" if not first else "")
                        log.info("Connected to %s in %.0f ms%s", self._model,
                                 (time.monotonic() - connect_started) * 1000, how)
                        self._session = session
                        self._ready.set()
                        failures = 0
                        first = False
                        await self._status("ready")
                        if fresh_restart:
                            fresh_restart = False
                            await self._emit(VoiceEvent("notice", {"text": "Voice reconnected. I've kept our recent "
                                                                           "conversation, so just carry on."}))
                        await self._flush_pending_text()
                        await self._receive_loop(session)  # returns on GoAway (reconnect) or close
                        self._ready.clear()  # closing now: text sent from here on waits for the next connection
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    if self._closing.is_set():
                        break
                    kind = classify_error(e, resuming) if isinstance(e, errors.APIError) else TRANSIENT
                    if kind == FATAL:
                        log.error("Gemini Live refused the session: %s", e)
                        await self._emit(VoiceEvent("error", {"message": _friendly_error(e), "fatal": True}))
                        break
                    failures += 1
                    if first and failures >= FIRST_CONNECT_ATTEMPTS:
                        log.error("Couldn't connect to Gemini Live after %d attempts: %s", failures, e)
                        await self._emit(VoiceEvent("error", {"message": UNREACHABLE, "fatal": True}))
                        break
                    if kind == STALE_HANDLE or (resuming and failures >= RESUME_ATTEMPTS):
                        # The server no longer has (or can't restore) the old session: start a new one. The system
                        # prompt carries the reading position and recent conversation, so little is lost.
                        log.warning("Couldn't resume the Gemini session (%s); starting a fresh session", e)
                        self._handle = None
                        fresh_restart = True
                    else:
                        log.warning("Live connection dropped (%s); retrying (attempt %d)", e, failures)
                finally:
                    self._ready.clear()
                    self._session = None
                    self._cancel_tool_tasks()
                if failures > MAX_RECONNECTS:
                    log.error("Giving up on the voice connection after %d attempts", failures)
                    await self._emit(VoiceEvent("error", {"message": "Lost the voice connection after several tries. "
                                                                     "Check your internet and tap the orb to start "
                                                                     "again.", "fatal": True}))
                    break
                if failures:
                    await asyncio.sleep(0.5 if fresh_restart else min(2 ** failures, 10))
        finally:
            watchdog.cancel()
            await self._status("closed")

    async def close(self) -> None:
        self._closing.set()
        session = self._session
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()

    async def _status(self, state: str, **extra) -> None:
        await self._emit(VoiceEvent("status", {"state": state, **extra}))

    async def _idle_watchdog(self) -> None:
        while not self._closing.is_set():
            await asyncio.sleep(10)
            if time.monotonic() - self._last_activity > self._idle_timeout:
                log.info("No activity for %ds; closing the voice session to save quota", self._idle_timeout)
                await self._status("idle")
                await self.close()

    def _touch(self) -> None:
        self._last_activity = time.monotonic()

    # ---- input -------------------------------------------------------------------------

    async def send_audio(self, pcm16: bytes) -> None:
        session = self._session
        if session is None or not self._ready.is_set():
            return  # audio during (re)connect is dropped; the user just repeats
        with contextlib.suppress(Exception):
            await session.send_realtime_input(audio=types.Blob(data=pcm16, mime_type=AUDIO_MIME))

    async def end_audio(self) -> None:
        session = self._session
        if session is not None and self._ready.is_set():
            with contextlib.suppress(Exception):
                await session.send_realtime_input(audio_stream_end=True)

    async def send_text(self, text: str) -> None:
        self._touch()
        session = self._session
        if session is None or not self._ready.is_set():
            self._pending_text.append(text)
            return
        try:
            await self._send_text_now(session, text)
        except Exception as e:  # the connection is closing (GoAway, drop): keep the text for the next one
            log.info("Couldn't send a message while the connection was closing (%s); sending it after reconnecting", e)
            self._pending_text.append(text)

    @staticmethod
    async def _send_text_now(session, text: str) -> None:
        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=text)]), turn_complete=True
        )

    async def _flush_pending_text(self) -> None:
        while self._pending_text and self._session is not None:
            text = self._pending_text.pop(0)
            try:
                await self._send_text_now(self._session, text)
            except Exception as e:
                log.info("Couldn't send a queued message (%s); trying again after reconnecting", e)
                self._pending_text.insert(0, text)
                return

    # ---- output ------------------------------------------------------------------------

    async def _receive_loop(self, session) -> None:
        while not self._closing.is_set():
            async for msg in session.receive():
                if msg.session_resumption_update:
                    upd = msg.session_resumption_update
                    if upd.resumable and upd.new_handle:
                        self._handle = upd.new_handle
                if msg.go_away:
                    log.info("GoAway received (time left %s); reconnecting with resumption", msg.go_away.time_left)
                    return
                if msg.tool_call:
                    for fc in msg.tool_call.function_calls or []:
                        task = asyncio.create_task(self._handle_tool_call(session, fc))
                        self._tool_tasks[fc.id or fc.name] = task
                if msg.tool_call_cancellation:
                    for call_id in msg.tool_call_cancellation.ids or []:
                        task = self._tool_tasks.pop(call_id, None)
                        if task:
                            task.cancel()
                if msg.server_content:
                    await self._handle_content(msg.server_content)

    async def _handle_content(self, sc: types.LiveServerContent) -> None:
        if sc.interrupted:
            await self._emit(VoiceEvent("interrupted"))
        if sc.input_transcription and sc.input_transcription.text:
            self._touch()
            await self._emit(VoiceEvent("transcript", {"role": "user", "text": sc.input_transcription.text}))
        if sc.model_turn:
            for part in sc.model_turn.parts or []:
                if part.inline_data and part.inline_data.data:
                    self._touch()
                    await self._emit(VoiceEvent("audio", audio=part.inline_data.data))
        if sc.output_transcription and sc.output_transcription.text:
            await self._emit(VoiceEvent("transcript", {"role": "model", "text": sc.output_transcription.text}))
        if sc.turn_complete:
            await self._emit(VoiceEvent("turn_complete"))

    async def _handle_tool_call(self, session, fc: types.FunctionCall) -> None:
        call_id = fc.id or fc.name
        args = dict(fc.args or {})
        self._touch()
        await self._emit(VoiceEvent("tool_call", {"id": call_id, "name": fc.name, "args": args}))
        try:
            result, ui = await self._run_tool(fc.name, args)
        except asyncio.CancelledError:
            await self._emit(VoiceEvent("tool_result", {"id": call_id, "name": fc.name, "cancelled": True}))
            raise
        except Exception as e:  # a tool bug must not kill the conversation
            log.exception("Tool %s failed", fc.name)
            result, ui = {"error": f"The tool failed: {e}"}, None
        finally:
            self._tool_tasks.pop(call_id, None)
        await self._emit(VoiceEvent("tool_result", {"id": call_id, "name": fc.name, "ui": ui,
                                                    "error": result.get("error")}))
        with contextlib.suppress(Exception):
            await session.send_tool_response(
                function_responses=[types.FunctionResponse(id=fc.id, name=fc.name, response=result)]
            )

    def _cancel_tool_tasks(self) -> None:
        for task in self._tool_tasks.values():
            task.cancel()
        self._tool_tasks.clear()
