"""Retention policy and backwards-compatible record views (no model calls)."""

from copy import deepcopy
from dataclasses import dataclass

DAY = 86400


class MemoryCapacityError(RuntimeError):
    """A write would exceed the configured store budget."""


@dataclass(frozen=True)
class RetentionPolicy:
    episode_idle_days: int = 90
    archive_grace_days: int = 30
    history_days: int = 90
    audit_days: int = 90
    max_bytes: int = 512 * 1024 * 1024
    cleanup_ratio: float = 0.8
    target_ratio: float = 0.7

    def __post_init__(self):
        for value in (
            self.episode_idle_days,
            self.archive_grace_days,
            self.history_days,
            self.audit_days,
            self.max_bytes,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("Retention limits must be positive integers")
        if not 0 < self.target_ratio < self.cleanup_ratio < 1:
            raise ValueError("Retention ratios must satisfy 0 < target < cleanup < 1")


def record_view(record):
    """Legacy records are unpinned; never rewrite facts or the source evidence."""
    result = deepcopy(record)
    result.setdefault("retention", {"pinned": False, "evidence": None})
    # v3/v4 rows stay in their old project/Agent boundary; never infer that
    # an old semantic record is globally shareable from its type alone.
    result["scope"].setdefault("level", "project")
    return result


def memory_view(memory):
    if memory is not None and memory.metadata.get("writer_version") == "v3":
        memory.metadata = deepcopy(memory.metadata)
        memory.metadata["record"] = record_view(memory.metadata["record"])
    return memory
