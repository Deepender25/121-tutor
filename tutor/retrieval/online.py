"""Online embeddings: Gemini Embedding 2 through the Gemini API (EMBEDDING_MODEL=online).

Same interface as the local embedders (model_name, dim, count_tokens, embed_documents, embed_query), for
devices that can't run EmbeddingGemma. Things the API does that this class accounts for:
  - A list of plain strings is merged into ONE embedding (gemini-embedding-2 is multimodal), so every text is
    sent as its own Content: one vector per text, identical to embedding it alone.
  - At most 100 texts per request.
  - task_type isn't supported; the task goes in the text, in the same format EmbeddingGemma uses
    ("title: … | text: …" for chunks, "task: search result | query: …" for questions).
  - Quota is counted per text (free tier: 100 a minute). A client-side limiter keeps under it, leaving headroom
    so a live question isn't stuck behind indexing; a 429 is retried after the delay the API asks for, except a
    daily limit, which is reported straight away.
Chunk sizes are counted with EmbeddingGemma's tokenizer (only its tokenizer.json, no model or torch).
"""

import logging
import re
import threading
import time
from collections import deque
from typing import Callable

import numpy as np

from tutor import config

log = logging.getLogger("embedder")

QUERY_PROMPT = "task: search result | query: {}"
DOCUMENT_PROMPT = "title: {} | text: {}"
MAX_PER_REQUEST = 100  # API limit (BatchEmbedContentsRequest)
DOCUMENT_BATCH = 20  # small enough for steady progress, large enough to be cheap
QUERY_HEADROOM = 5  # texts per minute indexing leaves free for live questions
_RETRY_IN = re.compile(r"retry in ([\d.]+)s", re.IGNORECASE)


class EmbeddingError(RuntimeError):
    """An online embedding request failed; the message is written for the person using the app.
    transient: no connection, the per-minute quota, a server error (may work later) vs. a wrong key or model."""

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class RateLimiter:
    """Sliding one-minute window over texts sent. Background work (indexing) stops `headroom` short of the limit."""

    def __init__(self, per_minute: int, headroom: int = QUERY_HEADROOM, clock=time.monotonic, sleep=time.sleep):
        self.per_minute = per_minute
        self.headroom = min(headroom, per_minute // 4)
        self._sent: deque[tuple[float, int]] = deque()
        self._blocked_until = 0.0
        self._lock = threading.Lock()
        self._clock, self._sleep = clock, sleep

    def _used(self, now: float) -> int:
        while self._sent and now - self._sent[0][0] >= 60:
            self._sent.popleft()
        return sum(n for _, n in self._sent)

    def acquire(self, n: int, background: bool) -> float:
        """Block until n texts may be sent; returns seconds waited."""
        cap = self.per_minute - (self.headroom if background else 0)
        n = min(n, cap)
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                wait = self._blocked_until - now
                if wait <= 0:
                    used = self._used(now)
                    if used + n <= cap:
                        self._sent.append((now, n))
                        return waited
                    # wait for enough of the oldest sends to leave the window
                    need, freed = used + n - cap, 0
                    for t, k in self._sent:
                        freed += k
                        if freed >= need:
                            wait = t + 60 - now
                            break
            wait = max(0.05, wait)
            self._sleep(wait)
            waited += wait

    def block(self, seconds: float) -> None:
        """The API said the quota is used up: nobody sends for this long."""
        with self._lock:
            self._blocked_until = max(self._blocked_until, self._clock() + seconds)


def _retry_delay(error) -> float | None:
    """Seconds the API asks us to wait (RetryInfo, or 'retry in 23.9s' in the message)."""
    details = getattr(error, "details", None) or {}
    for d in (details.get("error", {}) or {}).get("details", []) if isinstance(details, dict) else []:
        if str(d.get("@type", "")).endswith("RetryInfo") and d.get("retryDelay"):
            try:
                return float(str(d["retryDelay"]).rstrip("s"))
            except ValueError:
                pass
    m = _RETRY_IN.search(str(getattr(error, "message", "") or error))
    return float(m.group(1)) if m else None


def _is_daily_quota(error) -> bool:
    return "perday" in str(getattr(error, "details", "") or error).lower().replace("_", "")


class GeminiEmbedder:
    mode = "online"

    def __init__(self, model_name: str = config.ONLINE_EMBEDDING_MODEL, dim: int = config.EMBEDDING_DIM,
                 client=None, tokenizer=None, per_minute: int = config.ONLINE_EMBED_PER_MINUTE, sleep=time.sleep):
        self.model_name = model_name
        self.dim = dim
        self.label = "Online · Gemini Embedding 2" if model_name == "gemini-embedding-2" else f"Online · {model_name}"
        self._sleep = sleep
        self.limiter = RateLimiter(per_minute, sleep=sleep)
        if client is None:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=config.gemini_api_key(), http_options=types.HttpOptions(timeout=60_000))
        self.client = client
        self.tokenizer = tokenizer if tokenizer is not None else _load_tokenizer(config.TOKENIZER_MODEL)

    # ---- the API

    def _request(self, texts: list[str]) -> np.ndarray:
        from google.genai import types

        response = self.client.models.embed_content(
            model=self.model_name,
            contents=[types.Content(parts=[types.Part(text=t)]) for t in texts],  # one vector per text
            config=types.EmbedContentConfig(output_dimensionality=self.dim),
        )
        vectors = np.asarray([e.values for e in (response.embeddings or [])], dtype=np.float32)
        if vectors.shape != (len(texts), self.dim):
            raise EmbeddingError(f"The embedding service returned {vectors.shape[0]} vectors of size "
                                 f"{vectors.shape[1] if vectors.ndim == 2 else '?'} for {len(texts)} texts.")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)  # already unit length; keep that guaranteed
        return vectors / np.where(norms == 0, 1, norms)

    def _embed(self, texts: list[str], background: bool, attempts: int) -> np.ndarray:
        from google.genai import errors

        delay = 1.0
        for attempt in range(1, attempts + 1):
            self.limiter.acquire(len(texts), background)
            try:
                return self._request(texts)
            except errors.APIError as e:
                code = getattr(e, "code", None)
                if code == 429:
                    if _is_daily_quota(e):
                        raise EmbeddingError("The daily Gemini embedding quota for this API key is used up. "
                                             "Try again tomorrow, or set EMBEDDING_MODEL=offline in .env.") from e
                    wait = (_retry_delay(e) or 30.0) + 0.5
                    self.limiter.block(wait)
                    if attempt == attempts:
                        raise EmbeddingError("The Gemini embedding quota is used up for now. Try again in a minute.",
                                             transient=True) from e
                    log.warning("Embedding quota reached; waiting %.0fs as the API asks", wait)
                    continue  # acquire() waits out the block
                if code in (400, 401, 403, 404):
                    raise EmbeddingError(_explain(e)) from e
                if attempt == attempts:  # 5xx and the like
                    raise EmbeddingError(f"The Gemini embedding service failed: {e}", transient=True) from e
            except EmbeddingError:
                raise
            except Exception as e:  # network: no connection, timeout
                if attempt == attempts:
                    raise EmbeddingError("Couldn't reach the Gemini embedding service. Check your internet "
                                         f"connection. ({type(e).__name__}: {e})", transient=True) from e
            log.warning("Embedding request failed (attempt %d/%d); retrying in %.0fs", attempt, attempts, delay)
            self._sleep(delay)
            delay = min(delay * 2, 16)
        raise AssertionError("unreachable")

    # ---- the embedder interface

    def check(self) -> None:
        """One tiny request: a wrong key or model name shows up at startup instead of at the first upload."""
        self._embed([QUERY_PROMPT.format("connection check")], background=False, attempts=2)

    def count_tokens(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)

    def embed_documents(
        self,
        items: list[tuple[str, str]],
        show_progress: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
        batch_size: int = DOCUMENT_BATCH,
    ) -> np.ndarray:
        """items: (title, text) pairs. Title = the chunk's heading path, which gives section context."""
        texts = [DOCUMENT_PROMPT.format(title or "none", text) for title, text in items]
        batch = max(1, min(batch_size, MAX_PER_REQUEST, self.limiter.per_minute - self.limiter.headroom))
        parts = []
        for start in range(0, len(texts), batch):
            parts.append(self._embed(texts[start:start + batch], background=True, attempts=8))
            done = min(start + batch, len(texts))
            if on_progress:
                on_progress(done, len(texts))
            elif show_progress:
                print(f"  embedded {done}/{len(texts)}", flush=True)
        return np.concatenate(parts) if parts else np.zeros((0, self.dim), np.float32)

    def embed_query(self, query: str) -> np.ndarray:
        return self._embed([QUERY_PROMPT.format(query)], background=False, attempts=2)[0]


def _explain(error) -> str:
    text = str(getattr(error, "message", "") or error)
    if "API_KEY_INVALID" in str(error) or "API key not valid" in text:
        return "GEMINI_API_KEY isn't valid, so online embeddings can't work. Check the key in .env."
    if getattr(error, "code", None) == 404:
        return f"The embedding model '{config.ONLINE_EMBEDDING_MODEL}' isn't available for this API key."
    if getattr(error, "code", None) == 403:
        return f"This API key isn't allowed to use Gemini embeddings: {text}"
    return f"The Gemini embedding request was rejected: {text}"


def _load_tokenizer(model_name: str):
    """EmbeddingGemma's tokenizer.json alone (32 MB, public; cached after the first download)."""
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer

    try:
        path = hf_hub_download(model_name, "tokenizer.json", local_files_only=True)
    except Exception:  # first run: not downloaded yet
        try:
            path = hf_hub_download(model_name, "tokenizer.json")
        except Exception as e:
            raise EmbeddingError(f"Couldn't download the tokenizer used for chunking ({model_name}): {e}") from e
    return Tokenizer.from_file(path)
