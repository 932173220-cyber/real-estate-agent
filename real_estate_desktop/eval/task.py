"""Inspect AI adapter for the 800-sample benchmark.

The solver drives the shipped ``Runtime``; the scorer reads only the booleans
produced by :mod:`real_estate_desktop.eval.scorers`. Logs therefore contain
per-sample checks that can be traced back to a database row, not just a number.
"""
from __future__ import annotations

import json

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import CORRECT, INCORRECT, Score, Target, accuracy, scorer
from inspect_ai.solver import Generate, TaskState, solver

from .dataset import build_samples
from .runner import Evaluator, build_runtime
from .scorers import score_sample

_EVALUATORS: dict[str, Evaluator] = {}


def evaluator_for(mode: str) -> Evaluator:
    if mode not in _EVALUATORS:
        _EVALUATORS[mode] = Evaluator(build_runtime(mode))
    return _EVALUATORS[mode]


@solver
def workflow_solver(mode: str = 'scripted'):
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        sample = state.metadata['sample']
        observed = await evaluator_for(mode).run_sample(sample)
        result = score_sample(sample, observed)
        state.metadata['result'] = result
        state.output.completion = json.dumps({'passed': result['passed'], 'reasons': result['reasons']}, ensure_ascii=False)
        return state

    return solve


@scorer(metrics=[accuracy()])
def task_success():
    async def score(state: TaskState, target: Target) -> Score:
        result = state.metadata.get('result') or {}
        return Score(value=CORRECT if result.get('passed') else INCORRECT,
                     answer=str(result.get('passed', False)),
                     explanation='; '.join(result.get('reasons') or [])[:800])
    return score


@task
def property_assistant(per_category: int = 100, mode: str = 'scripted') -> Task:
    samples = build_samples(per_category)
    dataset = MemoryDataset([Sample(id=sample['id'], input=sample['text'], target='pass', metadata={'sample': sample})
                            for sample in samples], name='property-assistant-800')
    return Task(dataset=dataset, solver=workflow_solver(mode), scorer=task_success(),
                name='property-assistant', metadata={'per_category': per_category, 'mode': mode})
