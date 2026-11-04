"""Atomic structured-memory versions over the existing SQLite vector store.

The repository shares the store's connection/lock so a version change and its
vectors are committed together. Network/model work is done before commit().
"""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import nullcontext
from typing import Any

import numpy as np

from agent_memory.embeddings import TfidfEmbedder
from agent_memory.retention import DAY, MemoryCapacityError, memory_view, record_view
from agent_memory.store import Memory, MemoryStore


class ConcurrentMemoryUpdate(RuntimeError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


class VersionedRepository:
    def __init__(self, store: MemoryStore):
        self.store = store
        with store._lock, store._conn:
            store._conn.execute("""CREATE TABLE IF NOT EXISTS memory_write_tasks (
                namespace TEXT NOT NULL, task_key TEXT NOT NULL, input_hash TEXT NOT NULL,
                result TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY(namespace, task_key))""")
            store._conn.execute("""CREATE TABLE IF NOT EXISTS memory_retention_events (
                namespace TEXT NOT NULL, memory_id TEXT NOT NULL,
                created_at REAL NOT NULL, payload TEXT NOT NULL)""")

    def snapshot(self, namespace: str) -> tuple[str, list[Memory]]:
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT id, namespace, content, metadata, embedding, created_at, expires_at, embedding_version "
                "FROM memories WHERE namespace = ? AND json_extract(metadata, '$.writer_version') = 'v3' ORDER BY id",
                (namespace,),
            ).fetchall()
            state = [[r[0], r[2], r[3], r[5], r[6]] for r in rows]
            digest = hashlib.sha256(canonical(state).encode()).hexdigest()
            return digest, [memory_view(self.store._row_to_memory(r)) for r in rows]

    def task(self, namespace: str, key: str, input_hash: str) -> dict | None:
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT input_hash, result FROM memory_write_tasks WHERE namespace=? AND task_key=?",
                (namespace, key),
            ).fetchone()
        if row is None:
            return None
        if row[0] != input_hash:
            raise ValueError("A source message ID was reused for different input")
        return json.loads(row[1])

    def snapshot_many(self, namespaces):
        """Capture all read scopes under one lock for optimistic atomic writes."""
        with self.store._lock:
            expected, memories = {}, []
            for namespace in dict.fromkeys(namespaces):
                expected[namespace], rows = self.snapshot(namespace)
                memories.extend(rows)
            return expected, memories

    def profile_candidates(self, namespaces, attributes):
        """Identity-scoped settings lookup; do not read/refresh embedding blobs."""
        namespaces, attributes = list(dict.fromkeys(namespaces)), list(attributes)
        if not namespaces or not attributes:
            return []
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT id,namespace,content,metadata,NULL,created_at,expires_at,embedding_version "
                "FROM memories WHERE namespace IN (" + ",".join("?" for _ in namespaces) + ") "
                "AND json_extract(metadata,'$.writer_version')='v3' "
                "AND json_extract(metadata,'$.record.scope.level')='user' "
                "AND json_extract(metadata,'$.record.memory_type')='semantic' "
                "AND json_extract(metadata,'$.record.subject') IN ('user','current_user') "
                "AND json_extract(metadata,'$.record.attribute') IN ("
                + ",".join("?" for _ in attributes)
                + ")",
                (*namespaces, *attributes),
            ).fetchall()
            return [memory_view(self.store._row_to_memory(r)) for r in rows]

    def prepare_vectors(self, records: list[dict]) -> np.ndarray:
        texts = [r["content"] for r in records]
        with self.store._lock:
            if isinstance(self.store.embedder, TfidfEmbedder):
                for text in texts:
                    self.store.embedder.partial_fit(text)
            if not texts:
                return np.empty((0, self.store.embedder.dim), dtype=np.float32)
            vectors = np.asarray(self.store.embedder.embed(texts), dtype=np.float32)
            if (
                vectors.shape != (len(texts), self.store.embedder.dim)
                or not np.isfinite(vectors).all()
            ):
                raise ValueError("Invalid memory embedding batch")
            return vectors

    def commit(
        self,
        namespace: str,
        key: str,
        input_hash: str,
        expected: str,
        records: list[dict],
        targets: list[str],
        vectors: np.ndarray,
        result: dict,
        *,
        retention_changes=None,
        source=None,
        now=None,
        max_bytes=None,
        confirmed_ids=None,
        maintenance_policy=None,
        record_namespaces=None,
    ) -> dict:
        store = self.store
        now = store._now() if now is None else now
        with store._lock:
            with store._conn:
                store._conn.execute("BEGIN IMMEDIATE")
                existing = self.task(namespace, key, input_hash)
                if existing is not None:
                    return existing
                if isinstance(expected, dict):
                    actual, previous = self.snapshot_many(expected)
                else:
                    actual, previous = self.snapshot(namespace)
                if actual != expected:
                    raise ConcurrentMemoryUpdate("Memory changed during comparison; retry the turn")
                previous_by_id = {m.id: m for m in previous}
                changes = retention_changes or {}
                routes = record_namespaces or {r["id"]: namespace for r in records}
                allowed = set(expected) if isinstance(expected, dict) else {namespace}
                if any(routes[r["id"]] not in allowed for r in records):
                    raise ValueError("Write namespace is outside the validated snapshot")
                locations = {m.id: m.namespace for m in previous}
                locations.update(routes)
                for target in targets:
                    old = previous_by_id[target]
                    record = copy.deepcopy(old.metadata["record"])
                    if record["status"] != "active":
                        raise ConcurrentMemoryUpdate("The comparison target is no longer active")
                    record["status"] = "superseded"
                    lifecycle = {**old.metadata.get("lifecycle", {}), "superseded_at": now}
                    metadata = {**old.metadata, "record": record, "lifecycle": lifecycle}
                    store._conn.execute(
                        "UPDATE memories SET metadata=? WHERE id=? AND namespace=?",
                        (canonical(metadata), target, old.namespace),
                    )
                for index, record in enumerate(records):
                    store._conn.execute(
                        "INSERT INTO memories(id,namespace,content,metadata,embedding,embedding_version,created_at,expires_at) "
                        "VALUES (?,?,?,?,?,?,?,NULL)",
                        (
                            record["id"],
                            routes[record["id"]],
                            record["content"],
                            canonical(
                                {
                                    "writer_version": "v3",
                                    "schema_version": 5 if "level" in record["scope"] else 4,
                                    "record": record,
                                    "lifecycle": {
                                        "last_confirmed_at": now,
                                        **(
                                            {"superseded_at": now}
                                            if record["status"] == "superseded"
                                            else {}
                                        ),
                                    },
                                }
                            ),
                            vectors[index].tobytes(),
                            store._embedding_version(),
                            now,
                        ),
                    )
                for mid, retention in changes.items():
                    target_namespace = locations[mid]
                    row = store._conn.execute(
                        "SELECT metadata FROM memories WHERE id=? AND namespace=?",
                        (mid, target_namespace),
                    ).fetchone()
                    if row is None:
                        raise ConcurrentMemoryUpdate("Retention target no longer exists")
                    metadata = json.loads(row[0])
                    metadata["record"] = record_view(metadata["record"])
                    metadata["record"]["retention"] = copy.deepcopy(retention)
                    metadata["retention_source"] = copy.deepcopy(source)
                    lifecycle = metadata.setdefault("lifecycle", {})
                    if retention["pinned"]:
                        # Protection may restore an archived active event, never its validity/status.
                        lifecycle.pop("archived_at", None)
                        lifecycle["last_confirmed_at"] = now
                    store._conn.execute(
                        "UPDATE memories SET metadata=? WHERE id=? AND namespace=?",
                        (canonical(metadata), mid, target_namespace),
                    )
                    store._conn.execute(
                        "INSERT INTO memory_retention_events VALUES (?,?,?,?)",
                        (
                            target_namespace,
                            mid,
                            now,
                            canonical({"retention": retention, "source": source}),
                        ),
                    )
                for mid in set(confirmed_ids or []):
                    target_namespace = locations[mid]
                    row = store._conn.execute(
                        "SELECT metadata FROM memories WHERE id=? AND namespace=?",
                        (mid, target_namespace),
                    ).fetchone()
                    if row:
                        metadata = json.loads(row[0])
                        if metadata["record"]["status"] == "active":
                            lifecycle = metadata.setdefault("lifecycle", {})
                            lifecycle["last_confirmed_at"] = now
                            lifecycle.pop("archived_at", None)
                            store._conn.execute(
                                "UPDATE memories SET metadata=? WHERE id=? AND namespace=?",
                                (canonical(metadata), mid, target_namespace),
                            )
                store._conn.execute(
                    "INSERT INTO memory_write_tasks(namespace,task_key,input_hash,result,created_at) VALUES (?,?,?,?,?)",
                    (namespace, key, input_hash, canonical(result), now),
                )
                if isinstance(store.embedder, TfidfEmbedder) and records:
                    # Persist TF-IDF state in the SAME transaction (helper commits).
                    store._conn.execute(
                        "UPDATE embedder_state SET payload=? WHERE id=1",
                        (store.embedder.serialize(),),
                    )
                if maintenance_policy is not None:
                    # Apply this turn's explicit protection before automatic pruning.
                    for memory_namespace in sorted(allowed):
                        self.maintain(
                            memory_namespace, maintenance_policy, now, _within_transaction=True
                        )
                if max_bytes is not None and self.storage_usage()["used_bytes"] > max_bytes:
                    raise MemoryCapacityError(
                        "Memory store budget reached; protected memories were not deleted"
                    )
            return result

    def storage_usage(self):
        """SQLite allocated payload including indexes/audits; free pages are reusable."""
        with self.store._lock:
            connection = self.store._conn
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            pages = connection.execute("PRAGMA page_count").fetchone()[0]
            free = connection.execute("PRAGMA freelist_count").fetchone()[0]
            return {
                "used_bytes": (pages - free) * page_size,
                "allocated_bytes": pages * page_size,
                "reusable_bytes": free * page_size,
            }

    def maintain(self, namespace, policy, now, *, compact=False, _within_transaction=False):
        """Scope-bound pruning. Never evict active semantic or pinned records."""
        store = self.store
        archived, removed = [], set()
        with store._lock:
            with nullcontext() if _within_transaction else store._conn:
                if not _within_transaction:
                    store._conn.execute("BEGIN IMMEDIATE")
                _, memories = self.snapshot(namespace)
                for memory in memories:
                    record = memory.metadata["record"]
                    if record["retention"]["pinned"]:
                        continue
                    metadata = copy.deepcopy(memory.metadata)
                    lifecycle = metadata.setdefault("lifecycle", {})
                    if record["status"] != "active":
                        if "superseded_at" not in lifecycle:
                            # v3 did not record replacement time: start a conservative
                            # history window now instead of guessing the original creation time.
                            lifecycle["superseded_at"] = now
                            store._conn.execute(
                                "UPDATE memories SET metadata=? WHERE namespace=? AND id=?",
                                (canonical(metadata), namespace, memory.id),
                            )
                        since = lifecycle["superseded_at"]
                        if now - since >= policy.history_days * DAY:
                            removed.add(memory.id)
                        continue
                    since = lifecycle.get("last_confirmed_at", memory.created_at)
                    if (
                        record["memory_type"] == "episodic"
                        and now - since >= policy.episode_idle_days * DAY
                    ):
                        if lifecycle.get("archived_at") is None:
                            # Deterministic transition even when maintenance did not run on day 90.
                            lifecycle["archived_at"] = since + policy.episode_idle_days * DAY
                            archived.append(memory.id)
                            store._conn.execute(
                                "UPDATE memories SET metadata=? WHERE namespace=? AND id=?",
                                (canonical(metadata), namespace, memory.id),
                            )
                        if now - lifecycle["archived_at"] >= policy.archive_grace_days * DAY:
                            removed.add(memory.id)
                store._conn.execute(
                    "DELETE FROM memory_write_tasks WHERE namespace=? AND created_at<=?",
                    (namespace, now - policy.audit_days * DAY),
                )
                store._conn.execute(
                    "DELETE FROM memory_retention_events WHERE namespace=? AND created_at<=?",
                    (namespace, now - policy.audit_days * DAY),
                )
                pressure = (
                    self.storage_usage()["used_bytes"] >= policy.max_bytes * policy.cleanup_ratio
                )
                # Space pressure never silently shortens the recovery grace period.
                self._remove_rows(namespace, removed)
            if (
                not _within_transaction
                and (compact or pressure)
                and self.storage_usage()["reusable_bytes"]
            ):
                store._conn.execute("VACUUM")
            return {
                "archived": archived,
                "deleted": sorted(removed),
                "pressure": pressure,
                "target_reached": self.storage_usage()["used_bytes"]
                <= policy.max_bytes * policy.target_ratio,
                **self.storage_usage(),
            }

    def _remove_rows(self, namespace, ids):
        # Do not use forget(): automatic pruning removes only eligible rows,
        # never the protected/current descendants in a version chain.
        for mid in ids:
            self.store._conn.execute(
                "DELETE FROM memories WHERE namespace=? AND id=?", (namespace, mid)
            )
            self.store._conn.execute(
                "DELETE FROM memory_retention_events WHERE namespace=? AND memory_id=?",
                (namespace, mid),
            )
        if ids:
            # Remove audit payloads referencing removed IDs, keeping other idempotency entries.
            rows = self.store._conn.execute(
                "SELECT namespace,task_key,result FROM memory_write_tasks"
            ).fetchall()
            for task_namespace, key, payload in rows:
                result = json.loads(payload)
                referenced = set(result.get("memory_ids", [])) | set(
                    result.get("retention_ids", [])
                )
                referenced |= {d.get("target_memory_id") for d in result.get("decisions", [])}
                referenced |= {
                    d.get("target_memory_id") for d in result.get("retention_decisions", [])
                }
                if referenced & ids:
                    self.store._conn.execute(
                        "DELETE FROM memory_write_tasks WHERE namespace=? AND task_key=?",
                        (task_namespace, key),
                    )

    def forget(self, namespace: str, memory_id: str) -> bool:
        store = self.store
        with store._lock, store._conn:
            store._conn.execute("BEGIN IMMEDIATE")
            _, previous = self.snapshot(namespace)
            records = {m.id: m.metadata["record"] for m in previous}
            if memory_id not in records:
                return False
            removed = {memory_id}
            while True:
                linked = {
                    key
                    for key, r in records.items()
                    if key in removed or r["supersedes"] in removed
                }
                linked |= {
                    records[key]["supersedes"]
                    for key in removed
                    if records[key]["supersedes"] in records
                }
                if linked <= removed:
                    break
                removed |= linked
            store._conn.executemany(
                "DELETE FROM memories WHERE namespace=? AND id=?",
                [(namespace, key) for key in removed],
            )
            # A user-level record may have been created by a project-scoped task.
            # IDs are globally unique; remove only audit payloads referencing this chain.
            self._remove_rows(namespace, removed)
            # Write results contain the original text: remove this scope's audit
            # payloads on explicit forgetting, too, rather than retaining a copy.
            store._conn.execute("DELETE FROM memory_write_tasks WHERE namespace=?", (namespace,))
            store._conn.executemany(
                "DELETE FROM memory_retention_events WHERE namespace=? AND memory_id=?",
                [(namespace, key) for key in removed],
            )
            return True
