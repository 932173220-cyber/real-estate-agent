"""LLM-written, versioned long-term memory, independent of any Agent SDK."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from agent_memory.component import (
    MemoryComponent,
    MemoryContext,
    MemoryScope,
    RecallTrace,
    default_namespace,
)
from agent_memory.grounding import LONGMEMORY_CONTEXT_RULES
from agent_memory.profile import SETTING_ALIASES, ContextBuildTrace, ProfilePolicy, select_profile
from agent_memory.repository import VersionedRepository, canonical
from agent_memory.retention import DAY, RetentionPolicy, memory_view
from agent_memory.store import Memory

RESOURCES = Path(__file__).with_name("resources")
logger = logging.getLogger(__name__)


class MemoryContractError(ValueError):
    """An LLM result failed structural, provenance or decision validation."""


def _checker() -> FormatChecker:
    checker = FormatChecker()

    @checker.checks("date", raises=ValueError)
    def check_date(value):
        return not isinstance(value, str) or (
            bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)) and bool(date.fromisoformat(value))
        )

    @checker.checks("date-time", raises=ValueError)
    def check_datetime(value):
        if not isinstance(value, str):
            return True
        return (
            bool(
                re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value
                )
            )
            and datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None
        )

    return checker


def _load(name: str) -> dict:
    return json.loads((RESOURCES / name).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class StructuredWriteTrace:
    namespace: str
    memories: tuple[Memory, ...]
    latency_ms: float
    decisions: tuple[dict, ...] = ()
    skipped_duplicates: int = 0
    pending: int = 0
    replayed: bool = False
    retention_decisions: tuple[dict, ...] = ()
    retention_updated: int = 0


class LongTermMemoryPlugin(MemoryComponent):
    """Read user/project vectors; write via v5 extraction and atomic versions.

    chat(system_prompt, input_dict, json_schema) must return a JSON string or
    object. The host injects its model/client; importing this module loads no
    chat SDK. Only completed, human-authored turns belong in remember_turn().
    """

    def __init__(
        self,
        store,
        *,
        chat: Callable[[str, dict, dict], Any],
        top_k=5,
        min_score=0.35,
        diversity=0.35,
        comparison_top_k=12,
        max_retries=1,
        clock=None,
        retention_policy=None,
        share_across_agents=False,
        profile_policy=None,
    ):
        super().__init__(
            store,
            top_k=top_k,
            min_score=min_score,
            diversity=diversity,
            namespace_resolver=self._namespace,
        )
        if max_retries not in (0, 1, 2) or comparison_top_k <= 0:
            raise ValueError("Invalid writer retry/comparison limits")
        if not math.isfinite(min_score) or not -1 <= min_score <= 1:
            raise ValueError("min_score must be a finite cosine threshold in [-1, 1]")
        self.chat = chat
        self.max_retries = max_retries
        self.comparison_top_k = comparison_top_k
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.retention_policy = retention_policy or RetentionPolicy()
        if not isinstance(share_across_agents, bool):
            raise ValueError("share_across_agents must be a boolean")
        self.share_across_agents = share_across_agents
        self.profile_policy = profile_policy or ProfilePolicy()
        if not isinstance(self.profile_policy, ProfilePolicy):
            raise ValueError("profile_policy must be a ProfilePolicy")
        self._context_traces = []
        self.repository = VersionedRepository(store)
        self.prompt = (RESOURCES / "extractor-system-v5_1.md").read_text(encoding="utf-8")
        self.schema = _load("memory-extraction-v5.schema.json")
        self.record_schema = _load("memory-record-v5.schema.json")
        self.decision_schema = _load("memory-decisions-v4.schema.json")
        self._writer_lock = threading.RLock()
        self._maintenance_times = {}

    @staticmethod
    def _scope(address: MemoryContext) -> MemoryScope:
        project = getattr(
            address, "project_id", dict(getattr(address, "extras", ())).get("project_id")
        )
        return MemoryScope(
            user_id=address.user_id,
            tenant_id=address.tenant_id,
            agent_id=address.agent_id or "default",
            conversation_id=address.conversation_id or "",
            project_id=project,
        )

    @classmethod
    def _namespace(cls, address):
        scope = cls._scope(address)
        default_namespace(scope)  # Validate required user/conversation/project identifiers.
        # Encode optional IDs without sentinel collisions (None != "default").
        identity = [
            scope.tenant_id,
            scope.user_id,
            scope.agent_id,
            scope.project_id,
            scope.conversation_id if scope.user_id is None else None,
        ]
        return "memory-v3:" + hashlib.sha256(canonical(identity).encode()).hexdigest()

    def _layer_namespace(self, scope, level):
        scope = self._scope(scope)
        legacy = self._namespace(scope)  # Also validate host identity.
        if level == "project" and (not self.share_across_agents or scope.user_id is None):
            return legacy
        identity = [
            scope.tenant_id,
            scope.user_id,
            ["shared"]
            if self.share_across_agents and scope.user_id is not None
            else ["agent", scope.agent_id],
            scope.project_id if level == "project" else None,
            scope.conversation_id if scope.user_id is None else None,
        ]
        return f"memory-v5-{level}:" + hashlib.sha256(canonical(identity).encode()).hexdigest()

    def _read_namespaces(self, scope):
        scope = self._scope(scope)
        namespaces = [self._layer_namespace(scope, "project")]
        if scope.user_id is not None:
            namespaces.append(self._layer_namespace(scope, "user"))
        if self.share_across_agents and scope.user_id is not None:
            # Existing private data is visible only to its original Agent.
            # Opting into sharing never silently publishes old records.
            namespaces.append(self._namespace(scope))
            private_identity = [
                scope.tenant_id,
                scope.user_id,
                ["agent", scope.agent_id],
                None,
                None,
            ]
            namespaces.append(
                "memory-v5-user:" + hashlib.sha256(canonical(private_identity).encode()).hexdigest()
            )
        return list(dict.fromkeys(namespaces))

    @staticmethod
    def _record_scope(record):
        context = record["scope"]
        return context.get("level", "project"), context["project_id"]

    def _visible(self, memory, address):
        scope = self._scope(address)
        record = memory.metadata.get("record")
        if not record or memory.metadata.get("writer_version") != "v3":
            return False
        owner = record["scope"]
        if owner["user_id"] != scope.user_id:
            return False
        level, project = self._record_scope(record)
        if level == "user":
            return (
                scope.user_id is not None
                and project is None
                and record["memory_type"] == "semantic"
            )
        return level == "project" and project == scope.project_id

    def _now(self):
        instant = self.clock()
        if instant.tzinfo is None:
            raise ValueError("Clock must return a timezone-aware datetime")
        return instant

    def _valid(self, memory: Memory, *, future=False, include_archived=False) -> bool:
        record = memory.metadata.get("record")
        if (
            memory.metadata.get("writer_version") != "v3"
            or not record
            or record["status"] != "active"
        ):
            return False
        if memory.expires_at is not None and memory.expires_at <= self.store._now():
            return False
        lifecycle = memory.metadata.get("lifecycle", {})
        if not include_archived and (
            lifecycle.get("archived_at") is not None
            or (
                record["memory_type"] == "episodic"
                and not record.get("retention", {}).get("pinned", False)
                and self._now().timestamp() - lifecycle.get("last_confirmed_at", memory.created_at)
                >= self.retention_policy.episode_idle_days * DAY
            )
        ):
            return False
        now = self._now()
        for key in ("valid_from", "valid_to"):
            value = record["time"][key]
            if value is None:
                continue
            if len(value) == 10:
                # Date-only validity follows the recorded message's timezone.
                zone = datetime.fromisoformat(
                    record["time"]["recorded_at"].replace("Z", "+00:00")
                ).tzinfo
                bound = date.fromisoformat(value)
                current = now.astimezone(zone).date()
            else:
                bound = datetime.fromisoformat(value.replace("Z", "+00:00"))
                current = now
            if key == "valid_from" and not future and current < bound:
                return False
            if key == "valid_to" and current > bound:
                return False
        return True

    def search(self, query, scope, *, limit=None, exclude_ids=()):
        excluded = frozenset(exclude_ids)
        with self.store._lock:
            results = self.store.search(
                query,
                namespaces=self._read_namespaces(scope),
                limit=self.top_k if limit is None else limit,
                min_score=self.min_score,
                diversity=self.diversity,
                memory_filter=lambda m: (
                    m.id not in excluded and self._visible(m, scope) and self._valid(m)
                ),
            )
            for hit in results:
                memory_view(hit.memory)
            return results

    def recall(self, query, address, *, exclude_ids=()):
        started = time.perf_counter()
        results = tuple(self.search(query, address, exclude_ids=exclude_ids))
        trace = RecallTrace(
            query, self._namespace(address), results, (time.perf_counter() - started) * 1000
        )
        with self._trace_lock:
            self._recall_traces.append(trace)
        return trace

    def load_profile(self, address):
        """Read current user defaults afresh; never call an embedder or model."""
        scope = self._scope(address)
        self._namespace(scope)
        if (
            scope.user_id is None
            or not self.profile_policy.max_items
            or not self.profile_policy.max_chars
        ):
            return select_profile([], self.profile_policy)
        candidates = self.repository.profile_candidates(
            self._read_namespaces(scope), SETTING_ALIASES
        )
        return select_profile(
            (m for m in candidates if self._visible(m, scope) and self._valid(m)),
            self.profile_policy,
        )

    @property
    def context_traces(self):
        with self._trace_lock:
            return tuple(self._context_traces)

    def build_longmemory(self, query, address):
        """Return the named extension for a host's complete context object."""
        profile = self.load_profile(address)
        error_type = None
        recalled = ()
        projected = []
        try:
            # Remove supplied defaults and conflicting defaults before ranking,
            # so they neither repeat nor consume ordinary facts' top-k slots.
            trace = self.recall(
                query, address, exclude_ids=profile.covered_ids + profile.conflicting_ids
            )
            recalled = trace.results
            for result in recalled:
                record = result.memory.metadata["record"]
                entry = {
                    "id": result.memory.id,
                    "scope": {
                        "level": record["scope"].get("level", "project"),
                        "project_id": record["scope"]["project_id"],
                    },
                    "content": result.memory.content,
                }
                # Keep whole records and valid JSON, rather than cutting facts.
                if len(canonical(projected + [entry])) <= self.max_context_chars:
                    projected.append(entry)
        except Exception as exc:
            error_type = type(exc).__name__
            if not profile.memories and not profile.conflicting_settings:
                raise
            logger.warning("Related memory recall failed; user defaults retained (%s)", error_type)
        finally:
            with self._trace_lock:
                self._context_traces.append(
                    ContextBuildTrace(
                        tuple(m.id for m in profile.memories),
                        tuple(item["id"] for item in projected),
                        profile.conflicting_settings,
                        profile.omitted_by_budget,
                        error_type,
                    )
                )
        return {
            "longmemory": {
                "profile": json.loads(profile.payload),
                "memories": projected,
                "profile_conflicts": list(profile.conflicting_settings),
                "retrieval_status": "unavailable" if error_type else "ok",
            }
        }

    async def abuild_longmemory(self, query, address):
        return await asyncio.to_thread(self.build_longmemory, query, address)

    def recall_messages(self, query, address):
        """Compatibility hook: instructions plus explicit longmemory JSON."""
        context = self.build_longmemory(query, address)
        return [
            {
                "role": "system",
                "content": LONGMEMORY_CONTEXT_RULES
                + "\n\n"
                + json.dumps(context, ensure_ascii=False, separators=(",", ":")),
            }
        ]

    def list(self, scope, *, limit=10, include_history=False):
        _, memories = self.repository.snapshot_many(self._read_namespaces(scope))
        return sorted(
            (
                m
                for m in memories
                if self._visible(m, scope) and (include_history or self._valid(m))
            ),
            key=lambda m: m.created_at,
            reverse=True,
        )[:limit]

    def count(self, scope):
        _, memories = self.repository.snapshot_many(self._read_namespaces(scope))
        return sum(self._visible(m, scope) and self._valid(m) for m in memories)

    def get(self, memory_id, scope, *, include_history=False):
        with self.store._lock:
            memory = next(
                (
                    m
                    for ns in self._read_namespaces(scope)
                    if (m := memory_view(self.store.get(memory_id, namespace=ns))) is not None
                ),
                None,
            )
        if memory is None or memory.metadata.get("writer_version") != "v3":
            return None
        if not self._visible(memory, scope):
            return None
        return memory if include_history or self._valid(memory) else None

    def get_record(self, memory_id, scope, *, include_history=False):
        memory = self.get(memory_id, scope, include_history=include_history)
        return copy.deepcopy(memory.metadata["record"]) if memory else None

    def save(self, *args, **kwargs):
        raise ValueError(
            "Use remember_turn with trusted source metadata to save complete v3 records"
        )

    def update(self, *args, **kwargs):
        raise ValueError(
            "v3 updates must create a version through remember_turn; in-place text replacement is disabled"
        )

    def delete(self, memory_id, scope):
        with self._writer_lock:
            memory = self.get(memory_id, scope, include_history=True)
            return self.repository.forget(memory.namespace, memory_id) if memory else False

    def maintain(self, scope, *, compact=False):
        """Run this scope's retention rules; compact only in an idle host window."""
        with self._writer_lock:
            reports = [
                self.repository.maintain(ns, self.retention_policy, self._now().timestamp())
                for ns in self._read_namespaces(scope)
            ]
            if compact:
                with self.store._lock:
                    self.store._conn.execute("VACUUM")
            result = {
                **reports[-1],
                "archived": [mid for r in reports for mid in r["archived"]],
                "deleted": [mid for r in reports for mid in r["deleted"]],
                "pressure": any(r["pressure"] for r in reports),
                **self.repository.storage_usage(),
            }
            self._maintenance_times[self._namespace(scope)] = self._now().timestamp()
            return result

    def _call(self, prompt, inp, schema, extra_validation):
        validator = Draft202012Validator(schema, format_checker=_checker())
        for attempt in range(self.max_retries + 1):
            try:
                data = copy.deepcopy(inp)
                if attempt:
                    data["validation_feedback"] = (
                        "上一输出未通过结构、来源或决策校验。重新根据原始输入输出完整 JSON，不猜测 ID。"
                    )
                raw = self.chat(prompt, data, copy.deepcopy(schema))
                result = json.loads(raw) if isinstance(raw, str) else copy.deepcopy(raw)
                validator.validate(result)
                extra_validation(result)
                return result
            except (ValidationError, ValueError, TypeError, KeyError):
                if attempt == self.max_retries:
                    # Do not include provider content or the user's evidence in errors.
                    raise MemoryContractError("LLM output failed the memory contract") from None
        raise AssertionError("unreachable")

    def _extraction(self, user_input, response, scope, message_id, message_time, recent_context):
        ctx = {
            "scope": {
                "user_id": scope.user_id,
                "project_id": scope.project_id,
                "agent_id": scope.agent_id,
                "level": "project",
            },
            "allowed_memory_scopes": ["project", "user"]
            if scope.user_id is not None
            else ["project"],
            "conversation_id": scope.conversation_id,
            "recorded_at": self._now().isoformat(),
            "allocated_memory_ids": ["mem_" + uuid.uuid4().hex for _ in range(8)],
            "message_time": message_time,
            "timezone": str(datetime.fromisoformat(message_time.replace("Z", "+00:00")).tzinfo),
        }
        context = [
            {"role": m["role"], "content": m["content"][:2000]}
            for m in list(recent_context or [])[-6:]
            if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)
        ]
        inp = {
            "trusted_context": ctx,
            "target_user_message": {"id": message_id, "role": "user", "content": user_input},
            "recent_context": context + [{"role": "assistant", "content": response[:4000]}],
        }

        def validate(result):
            for index, r in enumerate(result["long_term_memory_candidates"]):
                if (
                    r["id"] != ctx["allocated_memory_ids"][index]
                    or r["scope"]["user_id"] != scope.user_id
                    or r["scope"]["agent_id"] != scope.agent_id
                    or r["source"]["conversation_id"] != scope.conversation_id
                    or r["source"]["message_id"] != message_id
                    or r["source"]["speaker"] != "user"
                    or r["time"]["recorded_at"] != ctx["recorded_at"]
                    or r["source"]["evidence"] not in user_input
                ):
                    raise ValueError("Metadata or evidence mismatch")
                level = r["scope"]["level"]
                if level == "user":
                    if (
                        scope.user_id is None
                        or r["scope"]["project_id"] is not None
                        or r["memory_type"] != "semantic"
                    ):
                        raise ValueError(
                            "Only a named user's general semantic facts may be user-scoped"
                        )
                elif r["scope"]["project_id"] != scope.project_id:
                    raise ValueError("Project memories must keep the trusted current project")
                start, end = r["time"]["valid_from"], r["time"]["valid_to"]
                evidence = r["retention"]["evidence"]
                if evidence is not None and evidence not in user_input:
                    raise ValueError("Retention evidence is not in the user message")
                if (
                    start
                    and end
                    and (
                        start[:10] > end[:10]
                        or (
                            len(start) > 10
                            and len(end) > 10
                            and datetime.fromisoformat(start.replace("Z", "+00:00"))
                            > datetime.fromisoformat(end.replace("Z", "+00:00"))
                        )
                    )
                ):
                    raise ValueError("Inverted validity interval")
            for request in result["retention_updates"]:
                if request["evidence"] not in user_input:
                    raise ValueError("Retention request evidence is not in the user message")

        return self._call(self.prompt, inp, self.schema, validate)

    def _compare(self, candidates, previous, scope, retention_requests=None):
        # Cover paraphrases via vector recall and exact subject/attribute slots.
        selected = {}
        retention_requests = retention_requests or []
        previous_by_id = {m.id: m for m in previous}
        fact_ids = {m.id for m in previous if self._valid(m, future=True, include_archived=True)}
        for r in candidates:
            for m in previous_by_id.values():
                old = m.metadata["record"]
                if (
                    m.id in fact_ids
                    and self._record_scope(old) == self._record_scope(r)
                    and (old["subject"], old["attribute"])
                    == (
                        r["subject"],
                        r["attribute"],
                    )
                ):
                    selected[m.id] = old
            with self.store._lock:
                hits = self.store.search(
                    r["content"],
                    namespaces=self._read_namespaces(scope),
                    limit=self.comparison_top_k,
                    min_score=-1.0,
                    memory_filter=lambda m, candidate=r: (
                        m.id in fact_ids
                        and self._record_scope(m.metadata["record"])
                        == self._record_scope(candidate)
                    ),
                )
            for hit in hits:
                if hit.memory.id in previous_by_id:
                    selected[hit.memory.id] = hit.memory.metadata["record"]
        for request in retention_requests:
            # Exact supplied IDs need no embedding and can refer to history.
            if request["target"] in previous_by_id:
                m = previous_by_id[request["target"]]
                selected[m.id] = m.metadata["record"]
            else:
                with self.store._lock:
                    hits = self.store.search(
                        request["target"],
                        namespaces=self._read_namespaces(scope),
                        limit=self.comparison_top_k,
                        min_score=-1.0,
                        memory_filter=lambda m: m.id in previous_by_id,
                    )
                for hit in hits:
                    selected[hit.memory.id] = previous_by_id[hit.memory.id].metadata["record"]
        if len(selected) > 100:
            raise MemoryContractError("Comparison context exceeds the configured safe limit")
        inp = {
            "candidates": candidates,
            "old_memories": list(selected.values()),
            "retention_requests": retention_requests,
            "fact_target_ids": sorted(fact_ids & selected.keys()),
        }
        planned = []
        targets = []
        changes = {}

        def validate(result):
            planned.clear()
            targets.clear()
            changes.clear()
            decisions = result["decisions"]
            if len(decisions) != len(candidates) or sorted(
                d["candidate_index"] for d in decisions
            ) != list(range(len(candidates))):
                raise ValueError("Exactly one decision is required per candidate")
            virtual = copy.deepcopy(selected)
            virtual_fact_ids = fact_ids & selected.keys()

            def retention_change(target, retention):
                if target in changes and changes[target] != retention:
                    raise ValueError("Contradictory retention requests need clarification")
                if virtual[target]["retention"]["pinned"] == retention["pinned"]:
                    raise ValueError("Unchanged retention must be NOOP")
                changes[target] = copy.deepcopy(retention)
                virtual[target]["retention"] = copy.deepcopy(retention)
                for record in planned:
                    if record["id"] == target:
                        record["retention"] = copy.deepcopy(retention)

            for d in sorted(decisions, key=lambda d: d["candidate_index"]):
                r = copy.deepcopy(candidates[d["candidate_index"]])
                action, target = d["action"], d["target_memory_id"]
                if action == "ADD" and target is not None:
                    raise ValueError("ADD cannot target an old memory")
                if action in ("NOOP", "UPDATE", "UPDATE_RETENTION") and (
                    target not in virtual
                    or target not in virtual_fact_ids
                    or virtual[target]["status"] != "active"
                ):
                    raise ValueError("Target was not supplied or is no longer active")
                if action == "PENDING" and target is not None and target not in virtual:
                    raise ValueError("Unknown PENDING target")
                if action in ("NOOP", "UPDATE", "UPDATE_RETENTION") and (
                    self._record_scope(virtual[target]) != self._record_scope(r)
                ):
                    raise ValueError("A fact decision cannot cross user/project memory scopes")
                if action == "ADD" and any(
                    all(
                        old[k] == r[k]
                        for k in ("memory_type", "subject", "attribute", "value", "content")
                    )
                    for old in virtual.values()
                    if old["status"] == "active"
                    and self._record_scope(old) == self._record_scope(r)
                ):
                    raise ValueError("ADD would duplicate an identical fact")
                if action == "UPDATE":
                    if r["retention"]["evidence"] is None:
                        r["retention"] = copy.deepcopy(virtual[target]["retention"])
                    virtual[target]["status"] = "superseded"
                    r["supersedes"] = target
                    if target in selected:
                        targets.append(target)
                    else:
                        next(p for p in planned if p["id"] == target)["status"] = "superseded"
                if action == "UPDATE_RETENTION":
                    if r["retention"]["evidence"] is None:
                        raise ValueError("Retention changes need explicit user evidence")
                    retention_change(target, r["retention"])
                if action == "NOOP" and r["retention"]["evidence"] is not None:
                    if virtual[target]["retention"]["pinned"] != r["retention"]["pinned"]:
                        raise ValueError("NOOP would drop an explicit retention change")
                if action in ("ADD", "UPDATE"):
                    Draft202012Validator(self.record_schema, format_checker=_checker()).validate(r)
                    planned.append(r)
                    virtual[r["id"]] = r
                    virtual_fact_ids.add(r["id"])

            retention_decisions = result["retention_decisions"]
            if len(retention_decisions) != len(retention_requests) or sorted(
                d["request_index"] for d in retention_decisions
            ) != list(range(len(retention_requests))):
                raise ValueError("Exactly one decision is required per retention request")
            for d in sorted(retention_decisions, key=lambda d: d["request_index"]):
                request = retention_requests[d["request_index"]]
                target, action = d["target_memory_id"], d["action"]
                if target is not None and target not in virtual:
                    raise ValueError("Unknown retention target")
                if action in ("UPDATE_RETENTION", "NOOP") and target is None:
                    raise ValueError("Retention action needs an existing target")
                if action == "UPDATE_RETENTION":
                    retention_change(
                        target, {"pinned": request["pinned"], "evidence": request["evidence"]}
                    )
                if action == "NOOP" and virtual[target]["retention"]["pinned"] != request["pinned"]:
                    raise ValueError("NOOP cannot lose an explicit retention request")

        result = self._call(
            (RESOURCES / "decision-system-v5_1.md").read_text(encoding="utf-8"),
            inp,
            self.decision_schema,
            validate,
        )
        return result["decisions"], planned, targets, changes, result["retention_decisions"]

    def remember_turn(
        self, user_input, response, address, *, message_id, message_time, recent_context=None
    ):
        scope = self._scope(address)
        if (
            not scope.conversation_id
            or not message_id
            or not user_input.strip()
            or not response.strip()
        ):
            raise ValueError("A complete human turn and trusted source IDs are required")
        checker = _checker()
        if not checker.conforms(message_time, "date-time"):
            raise ValueError("message_time must be an ISO 8601 time with timezone")
        namespace = self._namespace(scope)
        started = time.perf_counter()
        key = hashlib.sha256(
            canonical([scope.conversation_id, message_id, "extractor-system-v3"]).encode()
        ).hexdigest()
        input_hash = hashlib.sha256(user_input.encode()).hexdigest()
        with self._writer_lock:
            result = self.repository.task(namespace, key, input_hash)
            replayed = result is not None
            if result is None:
                now = self._now().timestamp()
                maintenance_due = (
                    now - self._maintenance_times.get(namespace, float("-inf")) >= DAY
                    or self.repository.storage_usage()["used_bytes"]
                    >= self.retention_policy.max_bytes * self.retention_policy.cleanup_ratio
                )
                expected, all_previous = self.repository.snapshot_many(self._read_namespaces(scope))
                previous = [m for m in all_previous if self._visible(m, scope)]
                extraction = self._extraction(
                    user_input, response, scope, message_id, message_time, recent_context
                )
                candidates, requests = (
                    extraction["long_term_memory_candidates"],
                    extraction["retention_updates"],
                )
                decisions, records, targets, changes, retention_decisions = (
                    self._compare(candidates, previous, scope, requests)
                    if candidates or requests
                    else ([], [], [], {}, [])
                )
                vectors = self.repository.prepare_vectors(records)
                # An update stays in its original physical privacy boundary;
                # enabling sharing must not move an old private version chain.
                routes = {m.id: m.namespace for m in previous}
                for record in records:
                    routes[record["id"]] = (
                        routes[record["supersedes"]]
                        if record["supersedes"]
                        else self._layer_namespace(scope, record["scope"]["level"])
                    )
                result = self.repository.commit(
                    namespace,
                    key,
                    input_hash,
                    expected,
                    records,
                    targets,
                    vectors,
                    {
                        "memory_ids": [r["id"] for r in records],
                        "decisions": decisions,
                        "retention_ids": list(changes),
                        "retention_decisions": retention_decisions,
                    },
                    retention_changes=changes,
                    source={
                        "conversation_id": scope.conversation_id,
                        "message_id": message_id,
                        "recorded_at": self._now().isoformat(),
                    },
                    now=self._now().timestamp(),
                    max_bytes=self.retention_policy.max_bytes,
                    confirmed_ids=[
                        d["target_memory_id"] for d in decisions if d["action"] == "NOOP"
                    ],
                    maintenance_policy=self.retention_policy if maintenance_due else None,
                    record_namespaces={r["id"]: routes[r["id"]] for r in records},
                )
                if maintenance_due:
                    self._maintenance_times[namespace] = self._now().timestamp()
            memories = tuple(
                m
                for mid in result["memory_ids"]
                if (m := self.get(mid, scope, include_history=True)) is not None
            )
        decisions = tuple(result["decisions"])
        retention_decisions = tuple(result.get("retention_decisions", []))
        trace = StructuredWriteTrace(
            namespace,
            memories,
            (time.perf_counter() - started) * 1000,
            decisions,
            sum(d["action"] == "NOOP" for d in (*decisions, *retention_decisions)),
            sum(d["action"] == "PENDING" for d in (*decisions, *retention_decisions)),
            replayed,
            retention_decisions,
            len(result.get("retention_ids", [])),
        )
        with self._trace_lock:
            self._write_traces.append(trace)
        return trace

    async def aremember_turn_with_source(self, user_input, response, address, **source):
        return await asyncio.to_thread(self.remember_turn, user_input, response, address, **source)

    async def aremember_turn(self, user_input, response, address, **source):
        return await self.aremember_turn_with_source(user_input, response, address, **source)
