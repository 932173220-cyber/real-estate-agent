"""Drive the real Runtime once per sample and collect observables.

The evaluator never re-implements the workflow: it calls ``Runtime.chat`` and
``Runtime.confirm`` exactly like the desktop app does, then reads the database
back. That keeps the benchmark honest — a test double cannot pass a check that
the product would fail.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ..config import Settings
from ..runtime import Runtime
from ..test_support import ScriptedModel
from .scorers import score_sample


def build_runtime(mode: str = 'scripted', settings=None) -> Runtime:
    """Build an isolated runtime; an evaluation never touches the app's data dir."""
    if settings is not None:
        return Runtime(settings, model=ScriptedModel() if mode == 'scripted' else None)
    if mode == 'scripted':
        return Runtime(Settings(Path(tempfile.mkdtemp(prefix='pa-eval-'))), model=ScriptedModel())
    from dataclasses import replace

    from ..config import load_settings

    live = load_settings(create_data_dir=False)
    return Runtime(replace(live, root=Path(tempfile.mkdtemp(prefix='pa-eval-'))))


class Evaluator:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.other = runtime.create_customer('评测隔离客户')['customer_id']
        self.other_session = runtime.new_session(self.other)

    async def run_sample(self, sample: dict) -> dict:
        r = self.runtime
        cid = r.create_customer(sample['customer'])['customer_id']
        sid = r.new_session(cid)
        observed = {'route': None, 'model_route': None, 'draft': None, 'properties': [], 'report': '', 'profile': {},
                    'followups': [], 'records_before': 0, 'isolation_blocked': False, 'error': None}
        try:
            out = await r.chat(sample['text'], cid, sid)
            observed['report'] = out.get('report') or ''
            observed['route'] = out.get('search_route')
            observed['model_route'] = out.get('model_route')
            draft = out.get('draft_id')
            observed['draft'] = bool(draft)
            observed['records_before'] = len(r.records(cid))
            if draft:
                rows = r.pending(cid)
                payload = json.loads(rows[0]['payload']) if rows else {}
                observed['properties'] = payload.get('properties') or []
                try:
                    await r.confirm(self.other, self.other_session, draft, True)
                except PermissionError:
                    observed['isolation_blocked'] = True
                await r.confirm(cid, sid, draft, True)
            observed['profile'] = r.profile(cid)
            observed['followups'] = [row['content'] for row in r.followups(cid)]
        except Exception as exc:  # a failing sample must not stop the run
            observed['error'] = f'{type(exc).__name__}: {exc}'[:300]
        return observed

    async def evaluate(self, samples, progress=None) -> list[dict]:
        results = []
        for index, sample in enumerate(samples, 1):
            observed = await self.run_sample(sample)
            scored = score_sample(sample, observed)
            results.append({'id': sample['id'], 'category': sample['category'], 'text': sample['text'],
                            'route': observed['route'], 'error': observed['error'], **scored})
            if progress:
                progress(index, len(samples), results[-1])
        return results


def _rate(results: list[dict], name: str) -> tuple[float, int]:
    rows = [row for row in results if name in row['checks']]
    if not rows:
        return 0.0, 0
    return sum(1 for row in rows if row['checks'][name]) / len(rows), len(rows)


def aggregate(results: list[dict], *, mode: str) -> dict:
    total = len(results)
    success = sum(1 for row in results if row['passed'])
    per_category: dict[str, dict] = {}
    for row in results:
        bucket = per_category.setdefault(row['category'], {'total': 0, 'passed': 0})
        bucket['total'] += 1
        bucket['passed'] += 1 if row['passed'] else 0
    hard, hard_n = _rate(results, 'constraints')
    memory, memory_n = _rate(results, 'memory')
    isolation, isolation_n = _rate(results, 'isolation')
    route, route_n = _rate(results, 'route')
    gate, gate_n = _rate(results, 'gate')
    honesty, honesty_n = _rate(results, 'honesty')
    failures: dict[str, int] = {}
    for row in results:
        for reason in row['reasons']:
            key = reason.split(':', 1)[0]
            failures[key] = failures.get(key, 0) + 1
    return {
        'mode': mode,
        'samples': total,
        'success_rate': round(success / total, 4) if total else 0.0,
        'route_accuracy': round(route, 4), 'route_samples': route_n,
        'hard_constraint_rate': round(hard, 4), 'hard_constraint_samples': hard_n,
        'memory_accuracy': round(memory, 4), 'memory_samples': memory_n,
        'isolation_rate': round(isolation, 4), 'isolation_samples': isolation_n,
        'draft_gate_rate': round(gate, 4), 'draft_gate_samples': gate_n,
        'honesty_rate': round(honesty, 4), 'honesty_samples': honesty_n,
        'per_category': {name: {**values, 'accuracy': round(values['passed'] / values['total'], 4)}
                         for name, values in sorted(per_category.items())},
        'failure_reasons': dict(sorted(failures.items(), key=lambda item: -item[1])),
    }
