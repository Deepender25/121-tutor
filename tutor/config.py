"""Central settings. Everything tunable lives here; override via environment variables."""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# .env in the project root; real environment variables take precedence.
load_dotenv(PROJECT_ROOT / ".env", override=False)

# Data lives outside OneDrive: Lance writes many small versioned files and sync clients lock them.
DATA_ROOT = Path(
    os.environ.get("VOICE_TUTOR_DATA")
    or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "voice-tutor"
)

# Embeddings. Changing the model means re-ingesting: one table never mixes embedding models.
EMBEDDING_MODEL = os.environ.get("VOICE_TUTOR_EMBEDDING_MODEL", "google/embeddinggemma-2")
EMBEDDING_DIM = 768
EMBEDDING_PROMPT_VERSION = 1

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
VAD_SILENCE_MS = int(os.environ.get("VOICE_TUTOR_VAD_SILENCE_MS", "1000"))
VAD_END_SENSITIVITY = os.environ.get("VOICE_TUTOR_VAD_END_SENSITIVITY", "low")  # "low" | "high"


def gemini_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set. Copy .env.example to .env and add your key.")
    return key


FILES_DIR = DATA_ROOT / "files"  # uploaded PDFs, stored as <doc_id>.pdf
NOTEBOOKS_DIR = DATA_ROOT / "notebooks"
SETTINGS_FILE = DATA_ROOT / "settings.json"

# Web app: the UI (Next.js, frontend/) is what you open; it forwards /api to the Python API server.
HOST = os.environ.get("VOICE_TUTOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("VOICE_TUTOR_PORT", "8765"))  # UI
API_PORT = int(os.environ.get("VOICE_TUTOR_API_PORT", "8766"))  # FastAPI (this package)
UI_URL = f"http://{HOST}:{PORT}"
API_URL = f"http://{HOST}:{API_PORT}"
MAX_UPLOAD_MB = 200
VOICES = ["Kore", "Puck", "Charon", "Zephyr", "Fenrir", "Leda", "Orus", "Aoede"]
DEFAULT_VOICE = "Kore"
LIVE_IDLE_TIMEOUT_S = 300  # close an idle Gemini session to save free-tier quota


def doc_dir(doc_id: str) -> Path:
    return DATA_ROOT / "docs" / doc_id
