"""Chroma-backed chunk store.

The SQLite document/chunk tables remain the source of truth for text, page
numbers and provenance. Chroma only owns the dense index, so a missing
``chromadb`` install (or a corrupted index) degrades to brute-force cosine
over the vectors SQLite keeps, instead of losing documents.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

COLLECTION = "knowledge_chunks"

# Chroma metadata values must be scalar; None is not accepted.
def _clean(metadata: dict) -> dict:
    return {key: ("" if value is None else value) for key, value in metadata.items() if isinstance(value, (str, int, float, bool)) or value is None}


class ChromaVectorStore:
    def __init__(self, path: str | Path, collection: str = COLLECTION) -> None:
        self.path = Path(path)
        self.collection_name = collection
        self.available = False
        self.reason = ""
        self._collection = None
        self._client = None
        self._ensure()

    def _ensure(self) -> None:
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            self.path.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(
                path=str(self.path),
                settings=ChromaSettings(anonymized_telemetry=False, allow_reset=False),
            )
            self._client = client
            self._collection = client.get_or_create_collection(
                name=self.collection_name, metadata={"hnsw:space": "cosine"}
            )
            self.available = True
        except Exception as exc:  # missing dependency or unwritable directory
            self.reason = f"{type(exc).__name__}: {exc}"
            logger.warning("Chroma unavailable (%s); dense search falls back to SQLite vectors", self.reason)

    def close(self) -> None:
        """Release the embedded store's file handles (Windows temp cleanup)."""
        try:
            if self._client is not None:
                self._client._system.stop()
        except Exception:
            pass
        try:
            # chromadb caches one System per settings hash; a stopped system
            # would otherwise poison the next client in the same process.
            from chromadb.api.client import Client as ChromaClient

            ChromaClient.clear_system_cache()
        except Exception:
            pass
        self._collection = None
        self._client = None
        self.available = False

    def reset(self) -> None:
        """Rebuild the collection after an embedding-space change.

        A deleted collection handle cannot be reused, so the embedded client is
        recreated as well; otherwise the stale handle silently loses upsert.
        """
        if not self.available:
            return
        try:
            self._client.delete_collection(self.collection_name)
        except Exception as exc:
            logger.warning("Chroma delete_collection failed: %s", exc)
        try:
            self._client._system.stop()
        except Exception:
            pass
        try:
            from chromadb.api.client import Client as ChromaClient

            ChromaClient.clear_system_cache()
        except Exception:
            pass
        self._client = None
        self._collection = None
        self.available = False
        self._ensure()

    def count(self) -> int:
        if not self.available:
            return 0
        try:
            return int(self._collection.count())
        except Exception as exc:
            logger.warning("Chroma count failed: %s", exc)
            return 0

    def upsert(self, ids: Sequence[str], embeddings, metadatas: Sequence[dict], documents: Sequence[str]) -> bool:
        if not self.available or not ids:
            return False
        try:
            self._collection.upsert(
                ids=list(ids),
                embeddings=[list(map(float, row)) for row in embeddings],
                metadatas=[_clean(meta) for meta in metadatas],
                documents=list(documents),
            )
            return True
        except Exception as exc:
            logger.warning("Chroma upsert failed: %s", exc)
            self.available = False
            return False

    def delete_path(self, path: str) -> None:
        if not self.available:
            return
        try:
            self._collection.delete(where={"path": path})
        except Exception as exc:
            logger.warning("Chroma delete failed: %s", exc)

    def delete_paths(self, paths) -> None:
        """Remove several documents' chunks in one call."""
        if not self.available or not paths:
            return
        try:
            self._collection.delete(where={"path": {"$in": list(paths)}})
        except Exception as exc:
            logger.warning("Chroma batch delete failed: %s", exc)

    def query(self, embedding, limit: int, where: dict | None = None) -> list[tuple[str, float]]:
        """Return ``(chunk_id, similarity)`` pairs ordered by similarity."""
        if not self.available or limit <= 0:
            return []
        try:
            result = self._collection.query(
                query_embeddings=[list(map(float, embedding))],
                n_results=limit,
                where=where or None,
                include=["distances"],
            )
        except Exception as exc:
            logger.warning("Chroma query failed: %s", exc)
            return []
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        return [(chunk_id, 1.0 - float(distance)) for chunk_id, distance in zip(ids, distances)]
