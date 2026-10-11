"""EmbeddingGemma wrapper. Applies the query vs document prompts explicitly so v1 and v2 behave the same.

Skipping these prompts silently hurts retrieval, so nothing outside this class calls the model directly.
"""

import threading
from typing import Callable

import numpy as np

from tutor import config

QUERY_PROMPT = "task: search result | query: {}"
DOCUMENT_PROMPT = "title: {} | text: {}"


class Embedder:
    mode = "offline"
    label = "Offline · EmbeddingGemma, on this computer"

    def __init__(self, model_name: str = config.OFFLINE_EMBEDDING_MODEL, dim: int = config.EMBEDDING_DIM):
        import torch
        from sentence_transformers import SentenceTransformer
        from transformers import AutoTokenizer

        extra = {}
        if "embeddinggemma-2" in model_name:
            # Text-only: skip the vision/audio encoders (740M -> ~270M params).
            extra["config_kwargs"] = {"vision_config": None, "audio_config": None}
        # float16 yields NaN/degraded embeddings for EmbeddingGemma; float32 is the safe CPU choice.
        def load(local_only: bool):
            model = SentenceTransformer(model_name, model_kwargs={"dtype": torch.float32},
                                        local_files_only=local_only, **extra)
            return model, AutoTokenizer.from_pretrained(model_name, local_files_only=local_only)

        try:
            # Cached copy first: no network round-trips to the Hub on every start.
            self.model, self.tokenizer = load(local_only=True)
        except Exception:
            self.model, self.tokenizer = load(local_only=False)  # first run: download
        self.model_name = model_name
        self.dim = dim
        # One model shared by the web server's threads (ingest jobs + live tool calls).
        self._lock = threading.Lock()

    def count_tokens(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False))

    def _encode(self, texts: list[str], batch_size: int = 16, show_progress: bool = False) -> np.ndarray:
        with self._lock:
            return self.model.encode(
                texts,
                batch_size=batch_size,
                normalize_embeddings=True,
                truncate_dim=self.dim,
                convert_to_numpy=True,
                show_progress_bar=show_progress,
            ).astype(np.float32)

    def embed_documents(
        self,
        items: list[tuple[str, str]],
        show_progress: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
        batch_size: int = 16,
    ) -> np.ndarray:
        """items: (title, text) pairs. Title = the chunk's heading path, which gives section context.
        Encodes in batches (releasing the lock in between so live searches aren't starved)."""
        texts = [DOCUMENT_PROMPT.format(title or "none", text) for title, text in items]
        if on_progress is None:
            return self._encode(texts, batch_size, show_progress)
        parts = []
        for start in range(0, len(texts), batch_size):
            parts.append(self._encode(texts[start:start + batch_size], batch_size))
            on_progress(min(start + batch_size, len(texts)), len(texts))
        return np.concatenate(parts) if parts else np.zeros((0, self.dim), np.float32)

    def embed_query(self, query: str) -> np.ndarray:
        return self._encode([QUERY_PROMPT.format(query)])[0]


def create_embedder(isolated: bool = False):
    """The embedder EMBEDDING_MODEL asks for. offline: EmbeddingGemma, in this process (command-line tools) or in its
    own crash-isolated process (isolated=True, the server). online: Gemini Embedding 2 over the API."""
    if config.EMBEDDING_MODE_ERROR:
        raise ValueError(config.EMBEDDING_MODE_ERROR)
    if config.EMBEDDING_MODE == "online":
        from tutor.retrieval.online import GeminiEmbedder

        return GeminiEmbedder()
    if isolated:
        from tutor.retrieval.worker import EmbedderClient

        return EmbedderClient()
    return Embedder()
