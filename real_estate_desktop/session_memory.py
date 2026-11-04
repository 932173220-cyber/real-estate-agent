"""Per-customer, per-conversation memory snapshots.

Snapshots live in the cache (Redis in production, in-process for tests and
demos), so a second window of the same customer sees confirmed changes without
paying for another vector recall. A rolling extractive summary of the turns
that fell out of the 8-round window is cached next to each snapshot.
"""
from __future__ import annotations
import asyncio,copy,re

from .cache import MemoryCache


class SessionMemoryContexts:
    def __init__(self, cache=None, *, ttl=3600, history_turns=8):
        self.cache = cache or MemoryCache()
        self.ttl = ttl
        self.history_turns = history_turns
        self.locks = {}

    def _key(self, ctx):
        return self.cache.k('sess', ctx.broker_id, ctx.customer_id, ctx.session_id)

    async def get(self, ctx, loader, *, refresh=False):
        key = self._key(ctx)
        async with self.locks.setdefault(key, asyncio.Lock()):
            if not refresh:
                cached = self.cache.get_json(key)
                if cached is not None:
                    cached['context_origin'] = 'session_cache'
                    cached['cache_backend'] = self.cache.backend
                    return cached
            value = await loader()
            self.cache.set_json(key, value, self.ttl)
            value = dict(value)
            value['context_origin'] = 'retrieved'
            value['cache_backend'] = self.cache.backend
            return value

    async def fallback(self, ctx, profile, versions=None, typed=None):
        key = self._key(ctx)
        payload = {'preferences': copy.deepcopy(profile), 'preference_versions': copy.deepcopy(versions or {}),
                   **copy.deepcopy(typed or {}), 'evidence': [], 'retrieval_status': 'unavailable'}
        async with self.locks.setdefault(key, asyncio.Lock()):
            self.cache.set_json(key, payload, self.ttl)

    async def update_customer(self, broker, cid, profile, versions=None, typed=None):
        """Refresh every cached conversation of this customer after a confirmed write."""
        prefix = self.cache.k('sess', broker, cid)
        for key in self.cache.keys(prefix):
            async with self.locks.setdefault(key, asyncio.Lock()):
                self.cache.set_json(key, {'preferences': copy.deepcopy(profile), 'preference_versions': copy.deepcopy(versions or {}),
                                          **copy.deepcopy(typed or {}), 'evidence': [], 'retrieval_status': 'profile_refreshed'}, self.ttl)

    async def summary(self, ctx, history, *, rebuild=False):
        """Structured summary of turns outside the window, refreshed as they fall out."""
        key = self.cache.k('summary', ctx.broker_id, ctx.customer_id, ctx.session_id)
        cached = self.cache.get_json(key)
        if cached is not None and not rebuild and cached.get('messages') == len(history):
            return cached
        payload = self._summarize(history)
        if history:
            self.cache.set_json(key, payload, self.ttl)
        return payload

    @staticmethod
    def _summarize(history):
        rejected, seen, questions, asks = [], [], [], []
        for row in history:
            content = (row.get('content') or '').strip()
            if not content:
                continue
            if row.get('role') == 'user':
                asks.append(re.sub(r'\s+', ' ', content)[:60])
                if re.search(r'不要|不接受|不考虑|排除|避开|别给我', content):
                    rejected.append(re.sub(r'\s+', ' ', content)[:40])
            else:
                questions.extend(re.findall(r'[^\n。]{0,20}？', content)[:1])
                seen.extend(re.findall(r'(?<![A-Za-z0-9_-])(?:P\d{3,}|[A-Z]{1,3}\d?-\d{2})(?![A-Za-z0-9_-])', content))
        parts = []
        if rejected:
            parts.append('已否决：' + '；'.join(dict.fromkeys(rejected))[:120])
        if seen:
            parts.append('看过：' + '、'.join(dict.fromkeys(seen))[:80])
        if questions:
            parts.append('待确认：' + '；'.join(dict.fromkeys(questions))[:100])
        if asks:
            parts.append('近期诉求：' + '；'.join(dict.fromkeys(asks[-2:]))[:100])
        return {'text': '；'.join(parts)[:300], 'messages': len(history)}

    def invalidate_customer(self, broker, cid):
        """Drop this customer's snapshots after a manual memory edit or deletion."""
        self.cache.invalidate(self.cache.k('sess', broker, cid))
        self.cache.invalidate(self.cache.k('summary', broker, cid))

    def invalidate_all(self):
        self.cache.invalidate(self.cache.k('sess'))
        self.cache.invalidate(self.cache.k('summary'))

    def clear(self):
        # Snapshots are meant to outlive the process when Redis is configured;
        # only the in-process locks are process-local.
        self.locks.clear()
        if isinstance(self.cache, MemoryCache):
            self.cache.clear()
