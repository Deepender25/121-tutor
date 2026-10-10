"""Voice previews for Settings: a short spoken sample per voice, generated once with Gemini Live (the same
voices the tutor speaks with) and cached as WAV files under DATA_ROOT/voice-previews/."""

import asyncio
import io
import logging
import wave

from google import genai
from google.genai import types

from tutor import config
from tutor.voice.base import OUTPUT_SAMPLE_RATE

log = logging.getLogger("voice.preview")
PREVIEW_DIR = config.DATA_ROOT / "voice-previews"
_locks: dict[str, asyncio.Lock] = {}


def preview_text(voice: str) -> str:
    return f"Hi, I'm {voice}. I'll be your tutor. Ask me anything about your notes, and let's learn it together."


def pcm_to_wav(pcm: bytes, rate: int = OUTPUT_SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


async def synthesize(voice: str) -> bytes:
    """Speak the preview line in `voice`; returns 24 kHz mono PCM16."""
    client = genai.Client(api_key=config.gemini_api_key())
    cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))),
        system_instruction="You are a voice actor reading one line. Say the user's text exactly, warmly and "
                           "naturally, in English. Say nothing else.",
    )
    pcm = bytearray()
    async with client.aio.live.connect(model=config.LIVE_MODEL, config=cfg) as session:
        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=preview_text(voice))]), turn_complete=True)
        async for msg in session.receive():
            sc = msg.server_content
            if sc and sc.model_turn:
                for part in sc.model_turn.parts or []:
                    if part.inline_data and part.inline_data.data:
                        pcm += part.inline_data.data
    if not pcm:
        raise RuntimeError("Gemini returned no audio for the preview")
    return bytes(pcm)


async def get_preview(voice: str) -> bytes:
    """WAV bytes for a voice's preview, generating and caching it on first use."""
    path = PREVIEW_DIR / f"{voice}.wav"
    if path.exists():
        return path.read_bytes()
    lock = _locks.setdefault(voice, asyncio.Lock())
    async with lock:  # two clicks at once generate it only once
        if path.exists():
            return path.read_bytes()
        log.info("Generating the %s voice preview ...", voice)
        wav = pcm_to_wav(await asyncio.wait_for(synthesize(voice), timeout=45))
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(wav)
        tmp.replace(path)
        log.info("Saved the %s voice preview (%.1fs)", voice, (len(wav) - 44) / (2 * OUTPUT_SAMPLE_RATE))
        return wav
