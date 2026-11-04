"""agent-memory: persistent memory for AI agents with vector similarity search."""

from agent_memory.chat import OpenAICompatibleJSONChat
from agent_memory.admin import ManualMemoryChange, MemoryAdmin
from agent_memory.component import MemoryComponent, MemoryScope, UserFactExtractor
from agent_memory.embeddings import (
    Embedder,
    SentenceTransformerEmbedder,
    TfidfEmbedder,
    ZhipuEmbedder,
)
from agent_memory.grounding import (
    ANSWER_GROUNDING_RULES,
    LONGMEMORY_CONTEXT_RULES,
    PROJECT_ASSISTANT_PROMPT,
)
from agent_memory.plugin import LongTermMemoryPlugin, MemoryContractError, StructuredWriteTrace
from agent_memory.profile import ContextBuildTrace, ProfilePolicy, ProfileSnapshot
from agent_memory.retention import MemoryCapacityError, RetentionPolicy
from agent_memory.store import Memory, MemoryStore, SearchResult

__version__ = "0.5.2"

__all__ = [
    "MemoryStore",
    "Memory",
    "SearchResult",
    "Embedder",
    "SentenceTransformerEmbedder",
    "TfidfEmbedder",
    "ZhipuEmbedder",
    "MemoryComponent",
    "MemoryScope",
    "UserFactExtractor",
    "LongTermMemoryPlugin",
    "MemoryContractError",
    "StructuredWriteTrace",
    "OpenAICompatibleJSONChat",
    "RetentionPolicy",
    "MemoryCapacityError",
    "ANSWER_GROUNDING_RULES",
    "PROJECT_ASSISTANT_PROMPT",
    "LONGMEMORY_CONTEXT_RULES",
    "ProfilePolicy",
    "ProfileSnapshot",
    "ContextBuildTrace",
    "MemoryAdmin",
    "ManualMemoryChange",
]
