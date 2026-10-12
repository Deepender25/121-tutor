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
from tutor.voice.style import clean_style

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
        self._reindex_lock = threading.Lock()
        self.auth = None  # AuthStore when VOICE_TUTOR_AUTH=on
        self.live_sessions = 0

    def load_embedder(self) -> None:
        started = time.perf_counter()
        if config.EMBEDDING_MODE == "online":
            log.info("Embeddings: online (%s via the Gemini API)", config.EMBEDDING_MODEL)
        else:
            log.info("Embeddings: offline. Loading the embedding model (%s) in its own process ...", config.EMBEDDING_MODEL)
        try:
            from tutor.retrieval.embedder import create_embedder
            from tutor.retrieval.worker import EmbeddingCrash

            try:
                embedder = create_embedder(isolated=True)
                if config.EMBEDDING_MODE == "online":
                    self._check_online(embedder)
                self.embedder = embedder
                self.cache = DocumentCache(self.embedder)
                log.info("Embeddings ready (%.1fs in total)", time.perf_counter() - started)
            except EmbeddingCrash as e:  # crashed even in safe mode; the reason is in the message
                log.error("%s", e)
                self.embedder_error = str(e)
        except Exception as e:
            log.error("Embeddings unavailable: %s", e, exc_info=not isinstance(e, (ValueError, RuntimeError)))
            self.embedder_error = str(e)
        finally:
            self.embedder_ready.set()
        if self.embedder is not None:
            self.reindex_missing()

    @staticmethod
    def _check_online(embedder) -> None:
        """A wrong key or model is an error now; no connection is only a warning (it may come back)."""
        from tutor.retrieval.online import EmbeddingError

        try:
            embedder.check()
        except EmbeddingError as e:
            if not e.transient:
                raise
            log.warning("Online embeddings: %s", e)

    # ---- embedding spaces ------------------------------------------------------------

    def reindex_missing(self) -> None:
        """Queue documents indexed with the other embedding model (EMBEDDING_MODEL changed) for re-embedding."""
        from tutor.library import missing_embeddings

        missing = missing_embeddings(self.embedder)
        if missing:
            log.info("%d document(s) were indexed with another embedding model; adding %s embeddings in the background",
                     len(missing), self.embedder.model_name)
        for doc_id in missing:
            self.ensure_embeddings(doc_id)

    def ensure_embeddings(self, doc_id: str) -> None:
        """Re-embed a document for the current model, unless that's already queued, running or failed."""
        from tutor.library import add_embeddings, read_meta

        with self._reindex_lock:
            if self.embedder is None or self.jobs.reindexing(doc_id):
                return
            meta = read_meta(doc_id)
            if meta is None:
                return

            def work(job) -> None:
                job.title = meta["title"]
                add_embeddings(doc_id, self.embedder, log=log.debug,
                               progress=lambda s, d, t: JobQueue.report(job, s, d, t))
                if self.cache:
                    self.cache.drop(doc_id)  # re-open with the new vectors

            self.jobs.submit("", meta["title"], work, kind="reindex", doc_id=doc_id)

    def start(self) -> None:
        for folder in (config.DATA_ROOT, config.FILES_DIR, config.NOTEBOOKS_DIR):
            folder.mkdir(parents=True, exist_ok=True)
        for leftover in config.FILES_DIR.glob("upload-*.part"):  # interrupted uploads
            leftover.unlink(missing_ok=True)
        if config.AUTH_ENABLED:
            from tutor.auth import AuthStore

            self.auth = AuthStore(config.AUTH_DB)
            purged = self.auth.purge_expired()
            accounts = self.auth.users()
            if not accounts:
                log.warning("Login is on but there's no account yet. Create it with: python manage.py user create <email>")
            else:
                log.info("Login is on (account: %s)%s", accounts[0].email,
                         f"; removed {purged} expired session(s)" if purged else "")
        threading.Thread(target=self.load_embedder, name="embedder-load", daemon=True).start()

    def stop(self) -> None:
        if self.embedder is not None and hasattr(self.embedder, "close"):
            self.embedder.close()
        if self.auth is not None:
            self.auth.close()

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
        return {"voice": voice, "style": clean_style(data.get("style"))}

    def settings(self) -> dict:
        with self._settings_lock:
            return self._read_settings()

    def update_settings(self, **changes) -> dict:
        with self._settings_lock:
            current = self._read_settings()  # NOT self.settings(): that would re-acquire the lock
            if changes.get("voice") in config.VOICES:
                current["voice"] = changes["voice"]
            if isinstance(changes.get("style"), dict):
                current["style"] = clean_style({**current["style"], **changes["style"]})
            config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = config.SETTINGS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(current, indent=1), encoding="utf-8")
            tmp.replace(config.SETTINGS_FILE)
            log.info("Settings saved: voice=%s, tutor %s", current["voice"],
                     ", ".join(f"{k}={v}" for k, v in current["style"].items()))
            return current

    @staticmethod
    def has_gemini_key() -> bool:
        return bool(os.environ.get("GEMINI_API_KEY", "").strip())
