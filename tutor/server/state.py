"""Process-wide state for the web app: embedder (loaded in the background), caches, stores, settings."""

import json
import logging
import os
import threading
import time

from tutor import config
from tutor.library import DocumentCache
from tutor.notebooks import NotebookStore
from tutor.server.jobs import JobQueue

log = logging.getLogger("state")


class AppState:
    def __init__(self):
        self.embedder = None
        self.embedder_error: str | None = None
        self.embedder_ready = threading.Event()
        self.cache: DocumentCache | None = None
        self.notebooks = NotebookStore(config.NOTEBOOKS_DIR)
        self.jobs = JobQueue()
        self._settings_lock = threading.RLock()  # re-entrant: a nested call can never deadlock

    def load_embedder(self) -> None:
        started = time.perf_counter()
        log.info("Loading the embedding model (%s) in its own process ...", config.EMBEDDING_MODEL)
        try:
            from tutor.retrieval.worker import EmbedderClient, EmbeddingCrash

            self.embedder = EmbedderClient()
            self.cache = DocumentCache(self.embedder)
            log.info("Embeddings ready (%.1fs in total)", time.perf_counter() - started)
        except EmbeddingCrash as e:  # crashed even in safe mode; the reason is in the message
            log.error("%s", e)
            self.embedder_error = str(e)
        except Exception as e:
            log.exception("Embedding model failed to load")
            self.embedder_error = str(e)
        finally:
            self.embedder_ready.set()

    def start(self) -> None:
        for folder in (config.DATA_ROOT, config.FILES_DIR, config.NOTEBOOKS_DIR):
            folder.mkdir(parents=True, exist_ok=True)
        for leftover in config.FILES_DIR.glob("upload-*.part"):  # interrupted uploads
            leftover.unlink(missing_ok=True)
        threading.Thread(target=self.load_embedder, name="embedder-load", daemon=True).start()

    def stop(self) -> None:
        if self.embedder is not None and hasattr(self.embedder, "close"):
            self.embedder.close()

    def require_embedder(self, timeout: float = 180):
        if not self.embedder_ready.wait(timeout):
            raise RuntimeError("The embedding model is still loading. Try again in a moment.")
        if self.embedder is None:
            raise RuntimeError(f"The embedding model failed to load: {self.embedder_error}")
        return self.embedder

    # ---- settings ------------------------------------------------------------------

    @staticmethod
    def _read_settings() -> dict:
        """Read settings.json without locking; callers hold the lock."""
        data = {}
        if config.SETTINGS_FILE.exists():
            try:
                data = json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        voice = data.get("voice") if data.get("voice") in config.VOICES else config.DEFAULT_VOICE
        return {"voice": voice}

    def settings(self) -> dict:
        with self._settings_lock:
            return self._read_settings()

    def update_settings(self, **changes) -> dict:
        with self._settings_lock:
            current = self._read_settings()  # NOT self.settings(): that would re-acquire the lock
            if changes.get("voice") in config.VOICES:
                current["voice"] = changes["voice"]
            config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = config.SETTINGS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(current, indent=1), encoding="utf-8")
            tmp.replace(config.SETTINGS_FILE)
            log.info("Settings saved: voice=%s", current["voice"])
            return current

    @staticmethod
    def has_gemini_key() -> bool:
        return bool(os.environ.get("GEMINI_API_KEY", "").strip())
