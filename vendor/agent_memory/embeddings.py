"""Embedding backends for agent-memory.

Three backends ship in-tree:

* ``SentenceTransformerEmbedder`` — high-quality dense embeddings. Used when
  the optional ``sentence-transformers`` dependency is installed.
* ``TfidfEmbedder`` — zero-dependency fallback built on top of numpy and the
  standard library. Good enough for small agent memory stores and keeps the
  package usable in slim environments (CI runners, serverless, edge).
* ``ZhipuEmbedder`` — hosted embedding-3 vectors, with credentials supplied
  at runtime and configuration-only serialization.

All implement the :class:`Embedder` protocol: ``embed(texts) -> np.ndarray``
where the returned matrix has shape ``(len(texts), dim)`` and rows are L2
normalized so that dot-product equals cosine similarity.
"""

from __future__ import annotations

import json
import os
import pickle
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import numpy as np

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]+")
_CJK_RE = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff]+$")


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text):
        token = raw.lower()
        if not _CJK_RE.fullmatch(token):
            tokens.append(token)
            continue

        # Chinese text is commonly written without whitespace. Character
        # unigrams retain broad recall while bigrams add phrase specificity.
        tokens.extend(token)
        tokens.extend(token[index : index + 2] for index in range(len(token) - 1))
    return tokens


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return matrix / norms


class Embedder(Protocol):
    """Minimal embedder interface used by :class:`MemoryStore`."""

    name: str
    dim: int
    version: int

    def embed(self, texts: Sequence[str]) -> np.ndarray: ...

    def serialize(self) -> bytes: ...

    @classmethod
    def deserialize(cls, payload: bytes) -> Embedder: ...


class ZhipuEmbeddingError(RuntimeError):
    """A sanitized embedding failure; never includes keys or response bodies."""


class ZhipuEmbedder:
    """Zhipu's hosted embedding-3 model, using the standard library HTTP client.

    Credentials stay in memory/environment and are never serialized to SQLite.
    All vectors are L2-normalized for cosine search and MMR.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "embedding-3",
        dimensions: int = 2048,
        base_url: str = "https://open.bigmodel.cn/api/paas/v4/",
        timeout: float = 30,
        batch_size: int = 16,
    ) -> None:
        if model != "embedding-3" or dimensions not in (256, 512, 1024, 2048):
            raise ValueError("Use embedding-3 with 256, 512, 1024, or 2048 dimensions")
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Invalid embedding endpoint")
        self.base_url = base_url.rstrip("/") + "/"
        key = api_key or os.getenv("ZHIPU_API_KEY")
        # A chat credential is reused only for the official Zhipu endpoint.
        if not key and parsed.hostname == "open.bigmodel.cn" and parsed.scheme == "https":
            chat_host = urlsplit(os.getenv("OPENAI_BASE_URL", "")).hostname
            if chat_host == "open.bigmodel.cn":
                key = os.getenv("OPENAI_API_KEY")
        if not key or not key.strip():
            raise ValueError("Configure ZHIPU_API_KEY or an official Zhipu chat credential")
        if timeout <= 0 or not 1 <= batch_size <= 64:
            raise ValueError("Invalid embedding timeout or batch size")
        self._api_key = key.strip()
        self.model = model
        self.dim = dimensions
        self.timeout = timeout
        self.batch_size = batch_size
        self.version = 0
        # Include endpoint and dimension to prevent mixing unrelated vector spaces.
        self.name = f"zhipu:{model}:{dimensions}:{self.base_url}"

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("Embedding texts must be non-empty strings")
        matrices = []
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            payload = json.dumps({"model": self.model, "input": batch, "dimensions": self.dim}).encode("utf-8")
            request = Request(
                self.base_url + "embeddings", data=payload,
                headers={"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    result = json.load(response)
            except HTTPError as exc:
                status = exc.code
                exc.close()
                raise ZhipuEmbeddingError(f"Zhipu embedding API returned HTTP {status}") from None
            except (URLError, TimeoutError, OSError):
                raise ZhipuEmbeddingError("Cannot connect to Zhipu embedding API") from None
            except (ValueError, UnicodeError):
                raise ZhipuEmbeddingError("Invalid embedding API response") from None
            try:
                items = sorted(result["data"], key=lambda item: item["index"])
                if [item["index"] for item in items] != list(range(len(batch))):
                    raise ValueError("Missing or duplicate indexes")
                matrix = np.asarray([item["embedding"] for item in items], dtype=np.float32)
                if matrix.shape != (len(batch), self.dim) or not np.isfinite(matrix).all():
                    raise ValueError("Invalid dimensions or values")
                if np.any(np.linalg.norm(matrix, axis=1) == 0):
                    raise ValueError("Zero embedding")
            except (KeyError, TypeError, ValueError):
                raise ZhipuEmbeddingError("Invalid embedding API vector shape or values") from None
            matrices.append(_l2_normalize(matrix))
        return np.vstack(matrices)

    def serialize(self) -> bytes:
        return json.dumps({"model": self.model, "dimensions": self.dim, "base_url": self.base_url}).encode("utf-8")

    @classmethod
    def deserialize(cls, payload: bytes) -> ZhipuEmbedder:
        return cls(**json.loads(payload))


class SentenceTransformerEmbedder:
    """Wrapper around ``sentence-transformers`` models.

    The model is only imported lazily so that importing :mod:`agent_memory`
    does not require the heavy dependency.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - exercised by users without the dep
            raise ImportError(
                "sentence-transformers is not installed. Install with "
                "`pip install agent-memory[st]` or fall back to TfidfEmbedder."
            ) from exc

        self.model_name = model_name
        self._model = SentenceTransformer(model_name)
        self.dim = int(self._model.get_sentence_embedding_dimension())
        self.name = f"st:{model_name}"
        self.version = 0

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vectors = self._model.encode(list(texts), convert_to_numpy=True, normalize_embeddings=True)
        return vectors.astype(np.float32)

    def serialize(self) -> bytes:
        return pickle.dumps({"model_name": self.model_name})

    @classmethod
    def deserialize(cls, payload: bytes) -> SentenceTransformerEmbedder:
        data = pickle.loads(payload)
        return cls(model_name=data["model_name"])


class TfidfEmbedder:
    """Simple hashing TF-IDF embedder with L2-normalized output.

    The vocabulary is learned from corpus text at :meth:`fit` time. Callers
    that want a pre-fitted embedder can pass a corpus at construction time.
    Dimension defaults to 512 which is a reasonable tradeoff for small agent
    memory stores (a few thousand entries) while keeping vectors cheap to
    store in SQLite.
    """

    def __init__(
        self,
        dim: int = 512,
        corpus: Iterable[str] | None = None,
    ) -> None:
        self.dim = dim
        self.name = f"tfidf:{dim}"
        # Document frequency per hashed bucket. Starts with a tiny smoothing
        # prior so the very first document still produces non-uniform IDF.
        self._df = np.ones(dim, dtype=np.float32)
        self._n_docs: int = 1
        if corpus is not None:
            self.fit(corpus)

    def _hash_token(self, token: str) -> int:
        # Deterministic, portable hash (Python's built-in hash is salted).
        h = 2166136261
        for ch in token.encode("utf-8"):
            h = ((h ^ ch) * 16777619) & 0xFFFFFFFF
        return h % self.dim

    def fit(self, corpus: Iterable[str]) -> TfidfEmbedder:
        for text in corpus:
            tokens = set(_tokenize(text))
            if not tokens:
                continue
            self._n_docs += 1
            for tok in tokens:
                self._df[self._hash_token(tok)] += 1.0
        return self

    def partial_fit(self, text: str) -> None:
        tokens = set(_tokenize(text))
        if not tokens:
            return
        self._n_docs += 1
        for tok in tokens:
            self._df[self._hash_token(tok)] += 1.0

    def _idf(self) -> np.ndarray:
        # Smooth IDF: log((N + 1) / (df + 1)) + 1
        return np.log((self._n_docs + 1.0) / (self._df + 1.0)).astype(np.float32) + 1.0

    @property
    def version(self) -> int:
        """Monotonic corpus generation used to detect stale stored vectors."""
        return self._n_docs

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        idf = self._idf()
        matrix = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            tokens = _tokenize(text)
            if not tokens:
                continue
            counts = Counter(self._hash_token(t) for t in tokens)
            total = float(sum(counts.values()))
            for bucket, cnt in counts.items():
                tf = cnt / total
                matrix[row, bucket] = tf * idf[bucket]
        return _l2_normalize(matrix)

    def serialize(self) -> bytes:
        return pickle.dumps(
            {
                "dim": self.dim,
                "df": self._df,
                "n_docs": self._n_docs,
            }
        )

    @classmethod
    def deserialize(cls, payload: bytes) -> TfidfEmbedder:
        data = pickle.loads(payload)
        emb = cls(dim=data["dim"])
        emb._df = data["df"].astype(np.float32)
        emb._n_docs = int(data["n_docs"])
        return emb


def default_embedder(prefer_sentence_transformers: bool = True) -> Embedder:
    """Return the best available embedder.

    Tries ``sentence-transformers`` first when ``prefer_sentence_transformers``
    is true, otherwise falls back to the TF-IDF embedder.
    """
    if prefer_sentence_transformers:
        try:
            return SentenceTransformerEmbedder()
        except ImportError:
            pass
    return TfidfEmbedder()


def cosine_similarity(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Cosine similarity between a single query vector and a matrix of rows.

    Assumes both inputs are already L2 normalized — which both embedders
    guarantee — so this reduces to a dot product.
    """
    if matrix.size == 0:
        return np.zeros((0,), dtype=np.float32)
    return matrix @ query.astype(np.float32)
