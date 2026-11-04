"""FastAPI REST server for :class:`MemoryStore`.

Run with::

    uvicorn agent_memory.server:app --reload

Or programmatically via :func:`create_app`::

    from agent_memory.server import create_app
    from agent_memory import MemoryStore

    app = create_app(MemoryStore("./memory.db"))

The server is intentionally thin — it mirrors the library API one-to-one so
that agents can speak to either transport interchangeably.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

try:
    from fastapi import Depends, FastAPI, HTTPException, Query
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover - the server extra is optional
    raise ImportError(
        "FastAPI is not installed. Install with `pip install agent-memory[server]`."
    ) from exc

from agent_memory.store import MemoryStore


class MemoryIn(BaseModel):
    content: str = Field(..., description="The memory text to store.")
    namespace: Optional[str] = Field(
        None, description="Optional namespace. Defaults to the store default."
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default_factory=dict, description="Arbitrary JSON metadata."
    )
    ttl: Optional[float] = Field(
        None, description="Seconds until the memory expires. Omit for no expiry."
    )
    id: Optional[str] = Field(None, description="Override the auto-generated id.")


class MemoryOut(BaseModel):
    id: str
    namespace: str
    content: str
    metadata: Dict[str, Any]
    created_at: float
    expires_at: Optional[float] = None


class SearchResultOut(BaseModel):
    memory: MemoryOut
    score: float


class StatsOut(BaseModel):
    total: int
    namespaces: List[str]
    embedder: str
    dim: int


def create_app(store: MemoryStore) -> FastAPI:
    app = FastAPI(
        title="agent-memory",
        description="Persistent vector memory for AI agents.",
        version="0.1.0",
    )

    def get_store() -> MemoryStore:
        return store

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/stats", response_model=StatsOut)
    def stats(s: MemoryStore = Depends(get_store)) -> StatsOut:
        return StatsOut(
            total=s.count(),
            namespaces=s.namespaces(),
            embedder=s.embedder.name,
            dim=s.embedder.dim,
        )

    @app.post("/memories", response_model=MemoryOut, status_code=201)
    def create_memory(body: MemoryIn, s: MemoryStore = Depends(get_store)) -> MemoryOut:
        try:
            mem = s.add(
                body.content,
                namespace=body.namespace,
                metadata=body.metadata,
                ttl=body.ttl,
                id=body.id,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return MemoryOut(**mem.to_dict())

    @app.get("/memories", response_model=list[MemoryOut])
    def list_memories(
        namespace: Optional[str] = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        s: MemoryStore = Depends(get_store),
    ) -> list[MemoryOut]:
        return [
            MemoryOut(**m.to_dict())
            for m in s.list(namespace=namespace, limit=limit, offset=offset)
        ]

    @app.get("/memories/search", response_model=list[SearchResultOut])
    def search_memories(
        q: str = Query(..., description="Query text."),
        namespace: Optional[str] = None,
        limit: int = Query(5, ge=1, le=100),
        min_score: float = Query(0.0, ge=-1.0, le=1.0),
        diversity: float = Query(0.0, ge=0.0, le=1.0),
        s: MemoryStore = Depends(get_store),
    ) -> list[SearchResultOut]:
        results = s.search(
            q,
            namespace=namespace,
            limit=limit,
            min_score=min_score,
            diversity=diversity,
        )
        return [
            SearchResultOut(memory=MemoryOut(**r.memory.to_dict()), score=r.score) for r in results
        ]

    @app.get("/memories/{memory_id}", response_model=MemoryOut)
    def get_memory(memory_id: str, s: MemoryStore = Depends(get_store)) -> MemoryOut:
        mem = s.get(memory_id)
        if mem is None:
            raise HTTPException(status_code=404, detail="memory not found")
        return MemoryOut(**mem.to_dict())

    @app.delete("/memories/{memory_id}", status_code=204)
    def delete_memory(memory_id: str, s: MemoryStore = Depends(get_store)) -> None:
        if not s.delete(memory_id):
            raise HTTPException(status_code=404, detail="memory not found")

    @app.post("/prune")
    def prune_memories(s: MemoryStore = Depends(get_store)) -> dict[str, int]:
        return {"pruned": s.prune()}

    return app


# Default application — backed by a store at AGENT_MEMORY_DB or ./agent_memory.db.
_default_path = os.environ.get("AGENT_MEMORY_DB", "agent_memory.db")
app = create_app(MemoryStore(_default_path))
