"""Run one capability scenario against the real product and collect observables."""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path

from ..config import Settings
from ..runtime import Runtime
from ..test_support import ScriptedModel


def build_runtime(mode: str = 'scripted', settings=None) -> Runtime:
    if settings is not None:
        return Runtime(settings, model=ScriptedModel() if mode == 'scripted' else None)
    if mode == 'scripted':
        return Runtime(Settings(Path(tempfile.mkdtemp(prefix='pa-cap-')), report_mode='explicit'), model=ScriptedModel())
    from dataclasses import replace

    from ..config import load_settings

    live = load_settings(create_data_dir=False)
    return Runtime(replace(live, root=Path(tempfile.mkdtemp(prefix='pa-cap-'))))


class ScenarioEvaluator:
    def __init__(self, runtime: Runtime, *, judge=None) -> None:
        self.runtime = runtime
        self.judge = judge

    # ------------------------------------------------------------------ helpers
    async def _chat(self, sample, cid, sid):
        out = await self.runtime.chat(sample['text'], cid, sid)
        draft = out.get('draft_id')
        result = None
        if draft:
            result = await self.runtime.confirm(cid, sid, draft, True)
        return out, result

    def _observed(self) -> dict:
        return {'route': None, 'model_route': None, 'draft': False, 'properties': [], 'report': '',
                'documents': [], 'profile': {}, 'records_before': 0, 'isolation_blocked': None, 'catalog': None,
                'turns': [], 'gate': {}, 'memory': {}, 'memory_records': {}, 'history': [],
                'error': None, 'denied': None, 'trace': [], 'duration_seconds': 0.0, 'tool_calls': 0,
                'tokens': 0, 'model_calls': 0, 'timeouts': 0, 'retries': 0,
                'redundant_calls': 0, 'tool_errors': 0}

    async def _drain(self):
        while self.runtime.background:
            await asyncio.gather(*tuple(self.runtime.background), return_exceptions=True)

    # ------------------------------------------------------------------ dispatch
    async def run(self, sample: dict) -> dict:
        handler = getattr(self, f"_run_{sample['kind']}", None)
        if handler is None:
            raise ValueError(f"unknown kind {sample['kind']}")
        observed = self._observed()
        cid = self.runtime.create_customer(sample['customer'])['customer_id']
        sid = self.runtime.new_session(cid)
        observed['customer_id'] = cid
        observed['session_id'] = sid
        events_before = len(getattr(self.runtime.model, 'generation_events', []) or [])
        started = time.perf_counter()
        try:
            await handler(sample, observed, cid, sid)
        except Exception as exc:  # a failing sample must not stop the run
            observed['error'] = f'{type(exc).__name__}: {exc}'[:300]
        finally:
            try:await self._drain()
            except Exception:pass
            observed['duration_seconds'] = round(time.perf_counter() - started, 3)
            trace = observed.get('trace') or []
            observed['tool_calls'] = len(trace)
            observed['timeouts'] = sum(1 for step in trace if step.get('error_code') == 'timeout')
            observed['retries'] = sum(1 for step in trace if int(step.get('attempts') or 1) > 1)
            seen_calls = set(); redundant = 0
            for step in trace:
                key = (step.get('tool'), json.dumps(step.get('args') or {}, sort_keys=True, ensure_ascii=False))
                if key in seen_calls: redundant += 1
                else: seen_calls.add(key)
            observed['redundant_calls'] = redundant
            observed['tool_errors'] = sum(1 for step in trace if step.get('status') == 'error')
            events = (getattr(self.runtime.model, 'generation_events', []) or [])[events_before:]
            observed['model_calls'] = len(events)
            observed['tokens'] = sum(int((event.get('usage') or {}).get('total_tokens') or 0) for event in events)
            try:observed['memory_records'] = self.runtime.memory_records(cid)
            except Exception:observed['memory_records'] = {}
            try:observed['history'] = self.runtime.history(cid, sid)
            except Exception:observed['history'] = []
        if self.judge is not None:
            try:observed['judge'] = await self.judge.score(sample, observed)
            except Exception as exc:observed['judge_error'] = f'{type(exc).__name__}: {exc}'[:200]
        return observed

    async def _run_chat(self, sample, observed, cid, sid):
        turns = sample.get('turns') or [sample['text']]
        draft_turns = [];final_draft = None
        for text in turns:
            out = await self.runtime.chat(text, cid, sid)
            final_draft = out.get('draft_id')
            draft_turns.append(bool(final_draft))
            observed['report'] = out.get('report') or ''
            observed['route'] = out.get('search_route')
            observed['model_route'] = out.get('model_route')
            observed['trace'] = out.get('trace') or observed['trace']
            observed['documents'] = out.get('documents') or observed['documents']
            observed['properties'] = out.get('properties') or observed['properties']
        observed['draft'] = bool(final_draft);observed['draft_turns'] = draft_turns
        # Snapshot before confirmation, otherwise the gate check trivially fails.
        observed['records_before'] = len(self.runtime.records(cid))
        if final_draft:
            rows = {row['draft_id']:row for row in self.runtime.pending(cid)}
            payload = json.loads(rows[final_draft]['payload']) if final_draft in rows else {}
            observed['properties'] = payload.get('properties') or []
            await self.runtime.confirm(cid, sid, final_draft, True)
        observed['profile'] = self.runtime.profile(cid)

    async def _run_memory(self, sample, observed, cid, sid):
        op = sample['op']

        async def drain():
            while self.runtime.background:
                await asyncio.gather(*tuple(self.runtime.background), return_exceptions=True)

        def snapshot():
            versions = self.runtime.preference_versions(cid)
            typed = self.runtime.memory_records(cid)
            return {'versions': {(v['key'], v['version']) for v in versions},
                    'active_versions': {v['key']: v['version'] for v in versions if v['active']},
                    'profile': self.runtime.profile(cid),
                    'records': len(typed['episodic']) + len(typed['semantic'])}

        async def turn(text):
            out = await self.runtime.chat(text, cid, sid)
            if out.get('draft_id'):
                await self.runtime.confirm(cid, sid, out['draft_id'], True)
            await drain()

        await turn(sample['text'])
        middle = snapshot()
        if op == 'add' or op == 'neutral':
            pass
        elif op == 'duplicate':
            await turn(sample['text'])
        elif op == 'update':
            second = (f"预算改为{sample['new_budget']}万" if sample.get('auto_memory')
                      else f"只更新偏好：预算改为{sample['new_budget']}万")
            await turn(second)
        else:
            await turn('这套房子周边配套怎么样？')
        after = snapshot()
        observed['memory'] = {'op': op, 'before': middle, 'auto_memory': sample.get('auto_memory', False), **after}
        observed['profile'] = after['profile']

    async def _run_catalog(self, sample, observed, cid, sid):
        service = self.runtime.catalog;added=None
        entry = service.create()
        try:
            row = await service.chat(entry['entry_id'], sample['text'])
            observed['catalog'] = {'status': row['status'], 'revision': row['revision'], 'payload': row.get('payload')}
            if row['status'] == 'pending':
                out = await service.confirm(entry['entry_id'], row['revision'], True)
                observed['catalog'].update(out);added=out.get('property_id')
                if added:
                    observed['catalog']['property'] = self.runtime.business.property(added)
                repeat = await service.confirm(entry['entry_id'], row['revision'], True)
                observed['catalog']['replayed'] = bool(repeat.get('replayed'))
        finally:
            # A real intake makes the listing search ignore demo fixtures for every
            # later sample in the same runtime, so undo the fixture here.
            if added:self.runtime.business.delete_property(added)

    async def _run_multi_turn(self, sample, observed, cid, sid):
        for turn_text in sample['turns']:
            out, _result = await self._chat({**sample, 'text': turn_text}, cid, sid)
            observed['turns'].append({'text': turn_text, 'report': out.get('report') or '',
                                      'route': out.get('search_route'), 'draft': bool(out.get('draft_id'))})
            observed['trace'] = out.get('trace') or observed['trace']
            observed['documents'] = out.get('documents') or observed['documents']
            observed['properties'] = out.get('properties') or observed['properties']
            observed.setdefault('profiles', []).append(self.runtime.profile(cid))
        observed['profile'] = self.runtime.profile(cid)
        observed['report'] = observed['turns'][-1]['report']
        observed['route'] = observed['turns'][-1]['route']

    async def _run_gate(self, sample, observed, cid, sid):
        turns = sample.get('turns') or [sample['text']]
        draft = None; draft_turns = []
        for text in turns:
            out = await self.runtime.chat(text, cid, sid)
            draft = out.get('draft_id')
            draft_turns.append(bool(draft))
            observed['route'] = out.get('search_route')
            observed['model_route'] = out.get('model_route')
            observed['trace'] = out.get('trace') or observed['trace']
            observed['report'] = out.get('report') or ''
            observed['documents'] = out.get('documents') or observed['documents']
            observed['properties'] = out.get('properties') or observed['properties']
        observed['draft_turns'] = draft_turns
        gate = {'records_after_chat': len(self.runtime.records(cid)), 'pending_after_chat': len(self.runtime.pending(cid)),
                'draft': bool(draft), 'draft_turns': draft_turns}
        if draft and sample['mode'] == 'double_confirm':
            first = await self.runtime.confirm(cid, sid, draft, True)
            second = await self.runtime.confirm(cid, sid, draft, True)
            gate.update(first=first.get('status'), replay=bool(second.get('replayed')),
                        records=len(self.runtime.records(cid)), followups=len(self.runtime.followups(cid)))
        elif draft and sample['mode'] == 'cancel':
            result = await self.runtime.confirm(cid, sid, draft, False)
            gate.update(cancelled=result.get('status'), records=len(self.runtime.records(cid)),
                        pending=len(self.runtime.pending(cid)))
        elif draft and sample['mode'] == 'no_confirm':
            gate.update(records=len(self.runtime.records(cid)), pending=len(self.runtime.pending(cid)))
        observed['gate'] = gate
        observed['draft'] = bool(draft)

    async def _run_followup(self, sample, observed, cid, sid):
        mode = sample.get('mode', 'generate')
        if mode == 'no_generate':
            before = len(self.runtime.followups(cid))
            out = await self.runtime.chat(sample['text'], cid, sid)
            observed['report'] = out.get('report') or ''
            observed['documents'] = out.get('documents') or observed['documents']
            observed['followup'] = {'draft': bool(out.get('draft_id')), 'before': before,
                                    'after': len(self.runtime.followups(cid))}
            observed['draft'] = bool(out.get('draft_id'))
            return
        for text in sample.get('turns') or [sample['text']]:
            out = await self.runtime.chat(text, cid, sid)
            if out.get('draft_id'):
                await self.runtime.confirm(cid, sid, out['draft_id'], True)
        before = len(self.runtime.followups(cid))
        out = await self.runtime.chat('生成跟进记录', cid, sid)
        draft = out.get('draft_id')
        observed['report'] = out.get('report') or ''
        observed['trace'] = out.get('trace') or []
        observed['documents'] = out.get('documents') or observed['documents']
        state = {'draft': bool(draft), 'before': before, 'mode': mode}
        if draft:
            result = await self.runtime.confirm(cid, sid, draft, mode != 'cancel')
            state['confirm'] = result.get('status')
        state['after'] = len(self.runtime.followups(cid))
        if state['after']:
            state['content'] = self.runtime.followups(cid)[0]['content']
        observed['followup'] = state
        observed['draft'] = bool(draft)

    async def _run_isolation(self, sample, observed, cid, sid):
        out = await self.runtime.chat(sample['text'], cid, sid)
        draft = out.get('draft_id')
        mode = sample['mode']
        if mode in ('cross_customer', 'cross_session'):
            # Explicit report mode means the first turn is only a conversation;
            # a real pending draft is needed to test cross-scope confirmation.
            report = await self.runtime.chat('整理成一份推荐报告', cid, sid)
            draft = report.get('draft_id') or draft
        observed['draft'] = bool(draft)
        if mode == 'authority':
            observed['authority_refused'] = bool(out.get('authority_refused'))
            observed['denied'] = observed['authority_refused']
            return
        if mode == 'cross_broker':
            try:
                self.runtime.business.customer('other-broker', cid)
                observed['denied'] = False
            except PermissionError:
                observed['denied'] = True
            return
        if mode == 'own_session':
            # Drafts are session-bound by design, so the legitimate control is
            # reading this customer's profile/history from a new conversation.
            try:
                self.runtime.profile(cid)
                self.runtime.sessions(cid)
                self.runtime.history(cid, self.runtime.new_session(cid))
                observed['own_scope_read_ok'] = True
            except Exception:
                observed['own_scope_read_ok'] = False
            observed['denied'] = False
            return
        other = self.runtime.create_customer('评测-隔离对象')['customer_id']
        other_session = self.runtime.new_session(other)
        target_session = other_session if mode == 'cross_customer' else self.runtime.new_session(cid)
        try:
            await self.runtime.confirm(other if mode == 'cross_customer' else cid, target_session, draft, True)
            observed['denied'] = False
        except PermissionError:
            observed['denied'] = True

    async def _run_fault(self, sample, observed, cid, sid):
        knowledge = self.runtime.knowledge
        original_search = knowledge.search
        if sample['mode'] == 'rag_error':
            def boom(*args, **kwargs):
                raise ConnectionError('injected rag failure')
            knowledge.search = boom
        else:
            original_business_search = self.runtime.business.search
            def slow(query):
                time.sleep(0.6)
                return original_business_search(query)
            self.runtime.business.search = slow
            self.runtime.tools.timeouts['search_listings'] = 0.15
        try:
            out = await self.runtime.chat(sample['text'], cid, sid)
            observed['report'] = out.get('report') or ''
            observed['route'] = out.get('search_route')
            observed['documents'] = out.get('documents') or observed['documents']
            observed['degraded_with_report'] = True
        except Exception as exc:
            observed['error'] = f'{type(exc).__name__}: {exc}'[:200]
            observed['degraded_with_report'] = False
        finally:
            knowledge.search = original_search
            if sample['mode'] != 'rag_error':
                self.runtime.business.search = original_business_search
            self.runtime.tools.timeouts['search_listings'] = 6
