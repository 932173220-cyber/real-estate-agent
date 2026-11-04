from __future__ import annotations
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

# The default embedding checkpoint ships next to the source tree, so a fresh
# data directory (or a test case) still uses the real model without a download.
_REPO_MODELS = Path(__file__).resolve().parent.parent / "models"
DEFAULT_EMBEDDING_MODEL = str(_REPO_MODELS / "bge-small-zh-v1.5") if (_REPO_MODELS / "bge-small-zh-v1.5").is_dir() else "BAAI/bge-small-zh-v1.5"

def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default

@dataclass(frozen=True)
class Settings:
    root: Path
    broker_id: str = 'local-broker'
    chat_api_key: str = ''
    chat_model: str = ''
    chat_base_url: str = 'https://open.bigmodel.cn/api/paas/v4/'
    embedding_backend: str = 'bge'
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    redis_url: str = os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0')
    history_turns: int = 8
    dense_top_k: int = 10
    lexical_top_k: int = 10
    rrf_k: int = 60
    retrieval_top_k: int = 3
    cache_ttl: int = 3600
    context_profile: str = 'compact'
    prompt_candidates: int = 8
    prompt_chunk_chars: int = 400
    history_max_tokens: int = 3000
    # Explicit in production (load_settings); tests built from Settings() keep the
    # legacy per-turn report so host-gate tests stay focused on their own logic.
    report_mode: str = 'auto'
    @property
    def data_dir(self):return self.root/'data'
    @property
    def business_db(self):return self.data_dir/'property.db'
    @property
    def knowledge_db(self):return self.data_dir/'knowledge.db'
    @property
    def memory_db(self):return self.data_dir/'customer_memory.db'
    @property
    def checkpoint_db(self):return self.data_dir/'workflows.db'
    @property
    def knowledge_dir(self):return self.data_dir/'knowledge_materials'
    @property
    def chroma_dir(self):return self.data_dir/'chroma'

def load_settings(root=None, *, create_data_dir=True):
    if root is None:
        root=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent.parent
        pointer=root/'房产助手位置.json'
        if pointer.exists():
            value=Path(json.loads(pointer.read_text('utf-8-sig'))['application_root'])
            if not value.is_absolute() or not value.is_dir():raise ValueError('房产助手位置文件中的目录不可用。')
            root=value
    root=Path(root).resolve();load_dotenv(root/'.env',encoding='utf-8-sig',override=False)
    if create_data_dir:root.joinpath('data').mkdir(parents=True,exist_ok=True)
    settings=Settings(root,os.getenv('PROPERTY_BROKER_ID','local-broker'),os.getenv('OPENAI_API_KEY',''),
        os.getenv('PROPERTY_MODEL','') or os.getenv('RESEARCH_MODEL','') or os.getenv('AGENTIFY_MODEL',''),
        os.getenv('OPENAI_BASE_URL','https://open.bigmodel.cn/api/paas/v4/'),
        os.getenv('EMBEDDING_BACKEND','bge'),
        os.getenv('EMBEDDING_MODEL',DEFAULT_EMBEDDING_MODEL),
        os.getenv('REDIS_URL','redis://127.0.0.1:6379/0'),
        _int('HISTORY_TURNS',8),_int('DENSE_TOP_K',10),_int('LEXICAL_TOP_K',10),_int('RRF_K',60),_int('RETRIEVAL_TOP_K',3),_int('CACHE_TTL',3600),
        os.getenv('CONTEXT_PROFILE','compact'),_int('PROMPT_CANDIDATES',8),_int('PROMPT_CHUNK_CHARS',400),_int('HISTORY_MAX_TOKENS',3000),os.getenv('REPORT_MODE','explicit'))
    validate_endpoint(settings.chat_base_url)
    return settings


# OpenAI-compatible endpoints the installer is willing to send a key to.
ALLOWED_ENDPOINT_HOSTS = frozenset({'open.bigmodel.cn', 'api.deepseek.com', 'api.openai.com'})
LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})


def validate_endpoint(base_url, *, allow_custom=None):
    """Reject credential-leaking or unapproved model endpoints.

    The key is a bearer token on every request, so a typo in ``OPENAI_BASE_URL``
    would mail it to whatever host was typed. Known providers and loopback
    (Ollama/vLLM) are allowed; anything else needs an explicit opt-in.
    """
    from urllib.parse import urlsplit

    if allow_custom is None:
        allow_custom = os.getenv('ALLOW_CUSTOM_BASE_URL', '') == '1'
    parts = urlsplit(base_url or '')
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('模型接口地址不能包含账号、查询参数或片段。')
    host = (parts.hostname or '').lower()
    if not host:
        raise ValueError('模型接口地址无效。')
    if parts.scheme == 'https' and (host in ALLOWED_ENDPOINT_HOSTS or allow_custom):
        return base_url
    if parts.scheme == 'http' and (host in LOOPBACK_HOSTS or allow_custom):
        return base_url
    raise ValueError('模型接口需使用受支持的 HTTPS 地址（智谱 / DeepSeek / OpenAI）；'
                     '本地兼容服务可用 http://127.0.0.1 或 http://localhost；'
                     '其他地址需显式设置 ALLOW_CUSTOM_BASE_URL=1。')



