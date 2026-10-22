"""Central settings. Everything tunable lives here; override via environment variables."""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# .env in the project root; real environment variables take precedence.
load_dotenv(PROJECT_ROOT / ".env", override=False)
os.environ.setdefault("LANCEDB_LOG", "error")  # lance's Rust logger prints a WARN on every new table


def _int_env(name: str, default: int) -> int:
    """A whole-number setting; a typo in .env stops the app with a plain sentence instead of a traceback."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a whole number (it is '{raw}'). Fix it in .env.") from None


def _choice_env(name: str, default: str, choices: tuple[str, ...]) -> str:
    """One of a few words, compared in lower case ('Low' is 'low')."""
    raw = os.environ.get(name, "")
    value = raw.strip().lower() or default
    if value not in choices:
        raise SystemExit(f"{name} must be one of {', '.join(choices)} (it is '{raw}'). Fix it in .env.")
    return value


# Data lives outside OneDrive: Lance writes many small versioned files and sync clients lock them.
DATA_ROOT = Path(
    os.environ.get("VOICE_TUTOR_DATA")
    or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "voice-tutor"
)

# Embeddings: EMBEDDING_MODEL=offline (default) runs EmbeddingGemma on this computer; EMBEDDING_MODEL=online uses
# Gemini Embedding 2 through GEMINI_API_KEY, for devices that can't run the local model. The two produce different
# vector spaces, so every document keeps one vector index per embedding space and a search only ever compares
# vectors from the same space (see library.py). Switching re-embeds the stored chunks in the background.
EMBEDDING_MODES = ("offline", "online")


def parse_embedding_mode(value: str | None) -> tuple[str, str | None]:
    """(mode, error). Unset or empty means offline; anything that isn't offline/online is an error."""
    mode = (value or "").strip().lower() or "offline"
    if mode not in EMBEDDING_MODES:
        return "offline", f"EMBEDDING_MODEL must be 'offline' or 'online' (it is '{value}'). Fix it in .env."
    return mode, None


EMBEDDING_MODE, EMBEDDING_MODE_ERROR = parse_embedding_mode(os.environ.get("EMBEDDING_MODEL"))
OFFLINE_EMBEDDING_MODEL = os.environ.get("VOICE_TUTOR_EMBEDDING_MODEL", "google/embeddinggemma-2")
ONLINE_EMBEDDING_MODEL = os.environ.get("VOICE_TUTOR_ONLINE_EMBEDDING_MODEL", "gemini-embedding-2")
EMBEDDING_MODEL = ONLINE_EMBEDDING_MODEL if EMBEDDING_MODE == "online" else OFFLINE_EMBEDDING_MODEL
EMBEDDING_DIM = 768  # both models: EmbeddingGemma natively, Gemini Embedding 2 via output_dimensionality
EMBEDDING_PROMPT_VERSION = 1  # same "title: … | text: …" / "task: search result | query: …" format for both
# Chunk sizes are measured with EmbeddingGemma's tokenizer in both modes (Gemma shares Gemini's tokenizer), so a
# document chunks the same way whichever mode indexed it. Online mode loads only the tokenizer file, never the model.
TOKENIZER_MODEL = OFFLINE_EMBEDDING_MODEL
# Online: requests per minute to stay under (the free tier allows 100 texts a minute; raise it on a paid tier).
ONLINE_EMBED_PER_MINUTE = max(10, _int_env("VOICE_TUTOR_ONLINE_EMBED_PER_MINUTE", 100))

# Chunking (tokens, measured with the embedding model's tokenizer).
CHUNK_TARGET_TOKENS = 550
CHUNK_MAX_TOKENS = 750
CHUNK_OVERLAP_TOKENS = 70
CHUNKER_VERSION = 1
INDEX_VERSION = 2  # bump when the LanceDB schema or FTS setup changes

# Sections synthesized every N pages when a PDF has neither an outline nor headings.
FALLBACK_SECTION_PAGES = 5

# Voice.
LIVE_MODEL = os.environ.get("VOICE_TUTOR_LIVE_MODEL", "gemini-3.8-live")
# Voice activity detection: how long a pause ends your turn. Higher = you can pause to think mid-sentence
# without the tutor jumping in (costs that much extra delay before each reply).
VAD_SILENCE_MS = _int_env("VOICE_TUTOR_VAD_SILENCE_MS", 1000)
VAD_END_SENSITIVITY = _choice_env("VOICE_TUTOR_VAD_END_SENSITIVITY", "low", ("low", "high"))
# How readily sound counts as you starting to talk (which interrupts the tutor). "low": a clear voice still does,
# but the tutor's own voice coming back through a phone speaker, or background noise, doesn't cut it off.
VAD_START_SENSITIVITY = _choice_env("VOICE_TUTOR_VAD_START_SENSITIVITY", "low", ("low", "high"))
# How long a sound must last before it counts as you starting to talk. Without it, on a phone speaker short bursts
# (the tutor's own voice coming back, a knock) cut the tutor off again and again and reached Gemini as one-syllable
# "words" ("bed", "pet", "net"): 13 interruptions in 2.5 minutes in one session. Speech that's meant to interrupt
# ("wait, wait") lasts longer than this. Lower it if very short answers get missed.
VAD_PREFIX_MS = _int_env("VOICE_TUTOR_VAD_PREFIX_MS", 300)
# Diagnostics for sound problems: save each voice session's audio in both directions (what the tutor sent, what
# the microphone sent) as WAV files with a timeline, in <data>/debug-audio, keeping the last few sessions.
# Off by default: it records your voice. Turn it on only while investigating, then off again.
RECORD_AUDIO = os.environ.get("VOICE_TUTOR_RECORD_AUDIO", "off").strip().lower() in ("on", "1", "true", "yes")


def gemini_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set. Copy .env.example to .env and add your key.")
    return key


# Logs: the project's logs/ folder locally; on a server somewhere the service may write (e.g. /var/log/voice-tutor).
LOG_DIR = Path(os.environ.get("VOICE_TUTOR_LOGS") or PROJECT_ROOT / "logs")

FILES_DIR = DATA_ROOT / "files"  # uploaded PDFs, stored as <doc_id>.pdf
NOTEBOOKS_DIR = DATA_ROOT / "notebooks"
DEBUG_AUDIO_DIR = DATA_ROOT / "debug-audio"  # VOICE_TUTOR_RECORD_AUDIO=on only
SETTINGS_FILE = DATA_ROOT / "settings.json"

# Web app: the UI (Next.js, frontend/) is what you open; it forwards /api to the Python API server.
HOST = os.environ.get("VOICE_TUTOR_HOST", "127.0.0.1")
PORT = _int_env("VOICE_TUTOR_PORT", 8765)  # UI
API_PORT = _int_env("VOICE_TUTOR_API_PORT", 8766)  # FastAPI (this package)
UI_URL = f"http://{HOST}:{PORT}"
API_URL = f"http://{HOST}:{API_PORT}"
MAX_UPLOAD_MB = _int_env("VOICE_TUTOR_MAX_UPLOAD_MB", 200)
MAX_LIVE_SESSIONS = _int_env("VOICE_TUTOR_MAX_LIVE_SESSIONS", 3)  # voice sessions open at once

# ---- Hosting (frontend on Vercel, API on a server). All off by default: locally nothing changes. ----
# The sites allowed to call this API from a browser (CORS + Origin checks), e.g. https://tutor.edgedrop.app.
PUBLIC_ORIGINS = [o.strip().rstrip("/") for o in os.environ.get("VOICE_TUTOR_PUBLIC_ORIGINS", "").split(",")
                  if o.strip()]
# Login (one email + password account, created with `python manage.py user create`). Required when hosted.
AUTH_ENABLED, AUTH_SETTING_ERROR = {"on": (True, None), "off": (False, None), "": (False, None)}.get(
    os.environ.get("VOICE_TUTOR_AUTH", "").strip().lower(),
    (True, "VOICE_TUTOR_AUTH must be 'on' or 'off'. Fix it in .env."))  # a typo fails closed: login stays on
# The session cookie is shared by the frontend and the API host: set it to the frontend's host
# (tutor.edgedrop.app also covers api.tutor.edgedrop.app). Empty = this host only (local use).
COOKIE_DOMAIN = os.environ.get("VOICE_TUTOR_COOKIE_DOMAIN", "").strip() or None
COOKIE_SECURE = os.environ.get("VOICE_TUTOR_COOKIE_SECURE", "on" if PUBLIC_ORIGINS else "off").strip().lower() == "on"
SESSION_COOKIE = "__Secure-vt_session" if COOKIE_SECURE else "vt_session"
AUTH_DB = DATA_ROOT / "auth.sqlite3"
# Where "/" on the API sends a browser: the hosted frontend, or the local UI.
APP_URL = PUBLIC_ORIGINS[0] if PUBLIC_ORIGINS else UI_URL


def is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")


def hosting_error() -> str | None:
    """Refuse to serve without login when the API can be reached by others."""
    if AUTH_SETTING_ERROR:
        return AUTH_SETTING_ERROR
    if not AUTH_ENABLED and (PUBLIC_ORIGINS or not is_loopback(HOST)):
        return ("Login is off but this API is reachable from other computers "
                f"({'VOICE_TUTOR_PUBLIC_ORIGINS is set' if PUBLIC_ORIGINS else f'VOICE_TUTOR_HOST={HOST}'}). "
                "Set VOICE_TUTOR_AUTH=on and create the account with: python manage.py user create <email>")
    return None
VOICES = ["Kore", "Puck", "Charon", "Zephyr", "Fenrir", "Leda", "Orus", "Aoede"]
DEFAULT_VOICE = "Kore"
LIVE_IDLE_TIMEOUT_S = 300  # close an idle Gemini session to save free-tier quota


def doc_dir(doc_id: str) -> Path:
    return DATA_ROOT / "docs" / doc_id
