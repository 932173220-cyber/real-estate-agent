"""Embedding backends for document, chunk and memory vectors.

BGE (``BAAI/bge-small-zh-v1.5``) is the production backend. Retrieval queries
carry the instruction prefix the model was trained with, document text does
not. A deterministic hashing embedder keeps the application usable when the
model files or the ``sentence-transformers`` dependency are unavailable, so a
deployment can degrade instead of crashing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Sequence

import numpy as np

logger = logging.getLogger(__name__)

# BGE zh models are trained asymmetrically: only queries get the instruction.
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

_MODELS: dict[str, "BgeEmbedder"] = {}
_MODEL_LOCK = threading.Lock()


class HashingEmbedder:
    """Deterministic character-feature fallback: no model, no network, no cache."""

    name = "property-local-char-v1"
    dim = 384
    version = 1

    def serialize(self) -> bytes:
        return b'{"version":1,"dim":384}'

    @classmethod
    def deserialize(cls, payload: bytes) -> "HashingEmbedder":
        if json.loads(payload) != {"version": 1, "dim": 384}:
            raise ValueError("Unsupported local feature format")
        return cls()

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        output = []
        for text in texts:
            vector = np.zeros(self.dim, dtype=np.float32)
            text = text.lower().replace(" ", "")
            for width in (1, 2):
                for i in range(max(0, len(text) - width + 1)):
                    gram = text[i : i + width]
                    value = int.from_bytes(hashlib.blake2s(gram.encode(), digest_size=4).digest(), "little")
                    vector[value % self.dim] += 1
            length = np.linalg.norm(vector)
            output.append(vector / length if length else vector)
        return np.asarray(output, dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed([text])[0]


class BgeEmbedder:
    """Sentence-transformers wrapper around a BGE checkpoint.

    Implements the ``agent_memory`` embedder protocol (``name``/``dim``/
    ``serialize``) so document chunks and customer memories share one vector
    space.
    """

    def __init__(self, model_path: str | Path, *, device: str | None = None, batch_size: int = 16) -> None:
        from sentence_transformers import SentenceTransformer

        self.model_path = str(model_path)
        self._model = SentenceTransformer(self.model_path, device=device)
        self.dim = int(self._model.get_sentence_embedding_dimension())
        self.batch_size = batch_size
        self.version = 1
        self.name = "bge:" + Path(self.model_path).name

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vectors = self._model.encode(
            list(texts), convert_to_numpy=True, normalize_embeddings=True,
            batch_size=self.batch_size, show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed([QUERY_INSTRUCTION + text])[0]

    def serialize(self) -> bytes:
        return json.dumps({"model_path": self.model_path, "dim": self.dim, "name": self.name}).encode("utf-8")

    @classmethod
    def deserialize(cls, payload: bytes) -> "BgeEmbedder":
        return cls(**json.loads(payload))


def load_embedder(settings) -> "BgeEmbedder | HashingEmbedder":
    """Return the configured embedder, falling back to the hashing backend.

    The model is process-wide: 139 workflow tests build their own ``Runtime``
    but must not reload 95 MB of weights for every case.
    """
    if getattr(settings, "embedding_backend", "bge") != "bge":
        return HashingEmbedder()
    model_path = str(getattr(settings, "embedding_model", "") or "")
    if not model_path:
        return HashingEmbedder()
    with _MODEL_LOCK:
        cached = _MODELS.get(model_path)
        if cached is not None:
            return cached
        # A missing local checkpoint would otherwise block on huggingface.co;
        # bound the metadata/download timeouts so offline hosts fail fast.
        os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "5")
        os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "10")
        try:
            embedder = BgeEmbedder(model_path)
        except Exception as exc:  # missing package, missing weights, bad path
            logger.warning("BGE embedder unavailable (%s: %s); falling back to hashing features", type(exc).__name__, exc)
            return HashingEmbedder()
        _MODELS[model_path] = embedder
        return embedder
