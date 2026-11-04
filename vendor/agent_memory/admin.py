"""Trusted local administration for structured long-term memories."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass

import numpy as np
from jsonschema import Draft202012Validator, FormatChecker

from agent_memory.repository import canonical


@dataclass(frozen=True, slots=True)
class ManualMemoryChange:
    action: str
    memory_id: str
    previous_id: str | None = None


class MemoryAdmin:
    """Scope-checked manual operations that preserve version and retention history."""

    def __init__(self, plugin) -> None:
        self.plugin = plugin

    def get(self, memory_id, scope):
        memory = self.plugin.get(memory_id.strip(), scope, include_history=True)
        if memory is None:
            raise KeyError("当前用户和项目下没有这条记忆")
        return memory

    @staticmethod
    def _manual_source(scope, action: str) -> dict:
        return {
            "type": "manual_cli",
            "action": action,
            "user_id": scope.user_id,
            "project_id": scope.project_id,
            "conversation_id": scope.conversation_id,
        }

    def edit(
        self,
        memory_id,
        scope,
        *,
        subject: str | None = None,
        attribute: str | None = None,
        value: str | None = None,
        content: str | None = None,
    ) -> ManualMemoryChange:
        with self.plugin._writer_lock:
            previous = self.get(memory_id, scope)
            old = previous.metadata["record"]
            if old["status"] != "active":
                raise ValueError("只能编辑当前有效版本")

            record = copy.deepcopy(old)
            replacements = {
                "subject": subject,
                "attribute": attribute,
                "value": value,
                "content": content,
            }
            for field, replacement in replacements.items():
                if replacement is not None:
                    replacement = replacement.strip()
                    if not replacement:
                        raise ValueError(f"{field} 不能为空")
                    record[field] = replacement

            if all(record[field] == old[field] for field in replacements):
                raise ValueError("内容没有变化")

            now = self.plugin._now()
            new_id = "mem_manual_" + uuid.uuid4().hex
            message_id = "manual_" + uuid.uuid4().hex
            record.update(
                {
                    "id": new_id,
                    "status": "active",
                    "supersedes": previous.id,
                    "source": {
                        "conversation_id": scope.conversation_id or "manual-cli",
                        "message_id": message_id,
                        "speaker": "user",
                        "evidence": record["content"],
                    },
                }
            )
            record["time"]["recorded_at"] = now.isoformat()
            Draft202012Validator(
                self.plugin.record_schema,
                format_checker=FormatChecker(),
            ).validate(record)

            expected, _ = self.plugin.repository.snapshot(previous.namespace)
            vectors = self.plugin.repository.prepare_vectors([record])
            task_key = "manual-edit-" + uuid.uuid4().hex
            input_hash = hashlib.sha256(canonical(record).encode()).hexdigest()
            source = self._manual_source(scope, "edit")
            result = {
                "memory_ids": [new_id],
                "decisions": [
                    {
                        "candidate_index": 0,
                        "action": "UPDATE",
                        "target_memory_id": previous.id,
                        "reason": "manual_cli",
                    }
                ],
                "retention_ids": [],
                "retention_decisions": [],
                "manual": source,
            }
            self.plugin.repository.commit(
                previous.namespace,
                task_key,
                input_hash,
                expected,
                [record],
                [previous.id],
                vectors,
                result,
                source=source,
                max_bytes=self.plugin.retention_policy.max_bytes,
                record_namespaces={new_id: previous.namespace},
            )
            return ManualMemoryChange("edit", new_id, previous.id)

    def set_pinned(self, memory_id, scope, pinned: bool) -> ManualMemoryChange:
        with self.plugin._writer_lock:
            memory = self.get(memory_id, scope)
            record = memory.metadata["record"]
            if record["status"] != "active":
                raise ValueError("只能修改当前有效版本的保留设置")
            if bool(record["retention"]["pinned"]) == pinned:
                return ManualMemoryChange("pin" if pinned else "unpin", memory.id)

            evidence = "用户通过长期记忆组件命令行标记为重要" if pinned else None
            retention = {"pinned": pinned, "evidence": evidence}
            expected, _ = self.plugin.repository.snapshot(memory.namespace)
            action = "pin" if pinned else "unpin"
            source = self._manual_source(scope, action)
            task_key = f"manual-{action}-" + uuid.uuid4().hex
            input_hash = hashlib.sha256(
                canonical([memory.id, retention, source]).encode()
            ).hexdigest()
            result = {
                "memory_ids": [],
                "decisions": [],
                "retention_ids": [memory.id],
                "retention_decisions": [
                    {
                        "request_index": 0,
                        "action": "UPDATE_RETENTION",
                        "target_memory_id": memory.id,
                        "reason": "manual_cli",
                    }
                ],
                "manual": source,
            }
            self.plugin.repository.commit(
                memory.namespace,
                task_key,
                input_hash,
                expected,
                [],
                [],
                np.empty((0, self.plugin.store.embedder.dim), dtype=np.float32),
                result,
                retention_changes={memory.id: retention},
                source=source,
            )
            return ManualMemoryChange(action, memory.id)

    def set_archived(self, memory_id, scope, archived: bool) -> ManualMemoryChange:
        with self.plugin._writer_lock:
            memory = self.get(memory_id, scope)
            record = memory.metadata["record"]
            if record["status"] != "active":
                raise ValueError("只能归档或恢复当前有效版本")
            if archived and record["retention"]["pinned"]:
                raise ValueError("重要记忆不能归档，请先取消重要标记")

            store = self.plugin.store
            now = self.plugin._now().timestamp()
            action = "archive" if archived else "restore"
            source = self._manual_source(scope, action)
            with store._lock, store._conn:
                store._conn.execute("BEGIN IMMEDIATE")
                row = store._conn.execute(
                    "SELECT metadata FROM memories WHERE namespace=? AND id=?",
                    (memory.namespace, memory.id),
                ).fetchone()
                if row is None:
                    raise KeyError("记忆已不存在")
                metadata = json.loads(row[0])
                lifecycle = metadata.setdefault("lifecycle", {})
                if archived:
                    lifecycle["archived_at"] = now
                else:
                    lifecycle.pop("archived_at", None)
                    lifecycle["last_confirmed_at"] = now
                store._conn.execute(
                    "UPDATE memories SET metadata=? WHERE namespace=? AND id=?",
                    (canonical(metadata), memory.namespace, memory.id),
                )
                store._conn.execute(
                    "INSERT INTO memory_retention_events VALUES (?,?,?,?)",
                    (
                        memory.namespace,
                        memory.id,
                        now,
                        canonical({"action": action, "source": source}),
                    ),
                )
            return ManualMemoryChange(action, memory.id)

    def delete(self, memory_id, scope) -> ManualMemoryChange:
        memory = self.get(memory_id, scope)
        if not self.plugin.delete(memory.id, scope):
            raise KeyError("记忆已不存在")
        return ManualMemoryChange("delete", memory.id)
