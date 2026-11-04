"""Bounded user defaults loaded by identity, without vector or model calls."""

from __future__ import annotations

import json
from dataclasses import dataclass

from agent_memory.store import Memory

# This is an allowlist of context settings, not a fact/importance classifier.
# General background, project configuration and events remain on-demand data.
SETTING_ALIASES = {
    "preferred_language": "preferred_language",
    "language": "preferred_language",
    "response_language": "preferred_language",
    "response_style": "response_style",
    "answer_style": "response_style",
    "response_length": "response_length",
    "verbosity": "response_length",
    "conciseness": "response_length",
    "preferred_name": "preferred_name",
    "preferred_address": "preferred_name",
    "output_format": "output_format",
    "preferred_format": "output_format",
    "timezone": "timezone",
    "time_zone": "timezone",
}


@dataclass(frozen=True)
class ProfilePolicy:
    max_items: int = 6
    max_chars: int = 1000

    def __post_init__(self):
        if any(
            isinstance(v, bool) or not isinstance(v, int) or v < 0
            for v in (self.max_items, self.max_chars)
        ):
            raise ValueError("Profile limits must be non-negative integers")


@dataclass(frozen=True)
class ProfileSnapshot:
    memories: tuple[Memory, ...] = ()
    payload: str = "[]"
    conflicting_settings: tuple[str, ...] = ()
    conflicting_ids: tuple[str, ...] = ()
    omitted_by_budget: int = 0
    covered_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextBuildTrace:
    profile_ids: tuple[str, ...]
    recalled_ids: tuple[str, ...]
    conflicting_settings: tuple[str, ...]
    omitted_by_budget: int
    vector_error_type: str | None


def setting_key(record):
    if (
        record["memory_type"] != "semantic"
        or record["scope"].get("level") != "user"
        or record["scope"]["project_id"] is not None
        or record["subject"] not in ("user", "current_user")
    ):
        return None
    return SETTING_ALIASES.get(record["attribute"])


def select_profile(memories, policy):
    """Never resolve conflicting active defaults by recency or vector score."""
    groups = {}
    for memory in memories:
        key = setting_key(memory.metadata["record"])
        if key:
            groups.setdefault(key, []).append(memory)
    selected, data, conflicts, conflict_ids, covered_ids = [], [], [], [], []
    omitted = 0
    for key in dict.fromkeys(SETTING_ALIASES.values()):
        group = groups.get(key, [])
        if not group:
            continue
        values = {m.metadata["record"]["value"].strip().casefold() for m in group}
        if len(values) > 1:
            conflicts.append(key)
            conflict_ids.extend(m.id for m in group)
            continue
        memory = max(group, key=lambda m: (m.created_at, m.id))
        entry = {
            "setting": key,
            "value": memory.metadata["record"]["value"],
            "memory_id": memory.id,
        }
        proposed = json.dumps(data + [entry], ensure_ascii=False, separators=(",", ":"))
        if len(selected) >= policy.max_items or len(proposed) > policy.max_chars:
            omitted += 1
            continue
        selected.append(memory)
        covered_ids.extend(m.id for m in group)
        data.append(entry)
    return ProfileSnapshot(
        tuple(selected),
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        tuple(conflicts),
        tuple(conflict_ids),
        omitted,
        tuple(covered_ids),
    )
