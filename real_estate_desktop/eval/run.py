"""Run a benchmark suite and write metrics that can be quoted.

    # 9 类能力场景（房源推荐/资料检索/客户记忆/资料录入/多轮对话/确认/跟进/隔离/降级）
    python -m real_estate_desktop.eval.run --suite capability --mode online --workers 6 --out eval_results/capability-900.json

    # 旧的检索路线回归集，保留用于回归对比
    python -m real_estate_desktop.eval.run --suite legacy --mode scripted --out eval_results/legacy-800.json

``--mode online`` needs a configured model key; without one only the scripted
pipeline run is possible, and the report says so.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .dataset import build_samples, summary_counts as legacy_counts
from .runner import Evaluator, aggregate, build_runtime
from .judge import GroundingJudge
from .scenario_runner import ScenarioEvaluator, build_runtime as build_capability_runtime
from .scenario_scorers import score_scenario
from .scenarios import build_scenarios, summary_counts as capability_counts


def _progress(index: int, total: int, row: dict) -> None:
    if index % 20 == 0 or index == total:
        print(f'[{index}/{total}] {"PASS" if row["passed"] else "FAIL"} {row["id"]}', flush=True)


def _round_robin(samples: list[dict], limit: int) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for sample in samples:
        buckets.setdefault(sample['category'], []).append(sample)
    picked: list[dict] = []
    while len(picked) < limit and any(buckets.values()):
        for name in sorted(buckets):
            if buckets[name] and len(picked) < limit:
                picked.append(buckets[name].pop(0))
    return picked


# --------------------------------------------------------------- legacy suite
async def _run_legacy(args) -> dict:
    samples = build_samples(args.per_category)
    if args.limit:
        samples = _round_robin(samples, args.limit)
    workers = max(1, min(args.workers, len(samples)))
    print(f'legacy samples={len(samples)} {legacy_counts(samples)} mode={args.mode} workers={workers}', flush=True)
    runtimes = [build_runtime(args.mode) for _ in range(workers)]
    evaluators = [Evaluator(runtime) for runtime in runtimes]
    shards = [samples[index::workers] for index in range(workers)]
    started = time.time()
    try:
        parts = await asyncio.gather(*(evaluator.evaluate(shard, progress=_progress)
                                       for evaluator, shard in zip(evaluators, shards)))
    finally:
        await asyncio.gather(*(runtime.close() for runtime in runtimes), return_exceptions=True)
    results = [row for part in parts for row in part]
    metrics = aggregate(results, mode=args.mode)
    metrics.update(suite='legacy', duration_seconds=round(time.time() - started, 1), workers=workers,
                   generated_at=datetime.now(timezone.utc).isoformat())
    return {'metrics': metrics, 'results': results}


# ---------------------------------------------------------- capability suite
def _aggregate_capability(results: list[dict], *, mode: str, repeats: int = 1) -> dict:
    total = len(results)
    success = sum(1 for row in results if row['passed'])
    grouped: dict[str, list[dict]] = {}
    for row in results:
        grouped.setdefault(row['id'], []).append(row)
    pass_k = (sum(1 for rows in grouped.values() if all(row['passed'] for row in rows)) / len(grouped)
              if grouped else 0.0)

    def rate(name: str) -> tuple[float, int]:
        rows = [row for row in results if name in row['checks']]
        if not rows:
            return 0.0, 0
        return sum(1 for row in rows if row['checks'][name]) / len(rows), len(rows)

    per_category: dict[str, dict] = {}
    for row in results:
        bucket = per_category.setdefault(row['category'], {'total': 0, 'passed': 0})
        bucket['total'] += 1
        bucket['passed'] += 1 if row['passed'] else 0
    hard, hard_n = rate('constraints')
    memory, memory_n = rate('memory_op')
    memory_faith, memory_faith_n = rate('memory_faithfulness')
    grounding, grounding_n = rate('grounding')
    isolation, isolation_n = rate('isolation')
    authority, authority_n = rate('authority')
    forbidden, forbidden_n = rate('plan_forbidden')
    degrade, degrade_n = rate('degraded')
    evidence, evidence_n = rate('evidence')
    gate, gate_n = rate('gate')
    plan, plan_n = rate('plan')
    fabrication_rows = [row for row in results if 'no_fabrication' in row['checks'] or 'honesty' in row['checks']]
    fabrication = (sum(1 for row in fabrication_rows if row['checks'].get('no_fabrication', row['checks'].get('honesty'))) / len(fabrication_rows)
                   if fabrication_rows else 0.0)
    safety_rows = [row for row in results if any(name in row['checks'] for name in ('gate', 'isolation', 'authority'))]
    safety = (sum(1 for row in safety_rows if all(row['checks'].get(name, True) for name in ('gate', 'isolation', 'authority'))) / len(safety_rows)
              if safety_rows else 0.0)
    judge_rows = [row['judge'] for row in results if row.get('judge')]
    def judge_mean(name):
        return round(sum(float(item.get(name) or 0) for item in judge_rows) / len(judge_rows), 4) if judge_rows else 0.0
    durations = sorted(float(row['duration_seconds']) for row in results if row.get('duration_seconds') is not None)
    tool_counts = sorted(int(row.get('tool_calls') or 0) for row in results)
    tokens = sorted(int(row.get('tokens') or 0) for row in results)
    model_calls = sorted(int(row.get('model_calls') or 0) for row in results)
    timeouts = sum(int(row.get('timeouts') or 0) for row in results)
    retries = sum(int(row.get('retries') or 0) for row in results)
    reused = sum(int(row.get('reused_calls') or 0) for row in results)
    redundant = sum(int(row.get('redundant_calls') or 0) for row in results)
    tool_errors = sum(int(row.get('tool_errors') or 0) for row in results)
    tool_total = sum(tool_counts) or 1
    def p95(values):
        return values[min(len(values) - 1, max(0, int(len(values) * 0.95) - 1))] if values else 0.0
    per_polarity: dict[str, dict] = {}
    for row in results:
        bucket = per_polarity.setdefault(row.get('polarity', 'positive'), {'total': 0, 'passed': 0})
        bucket['total'] += 1
        bucket['passed'] += 1 if row['passed'] else 0
    failures: dict[str, int] = {}
    for row in results:
        for reason in row['reasons']:
            key = reason.split(':', 1)[0].split(' ')[0]
            failures[key] = failures.get(key, 0) + 1
    return {
        'suite': 'capability', 'mode': mode, 'samples': len(grouped), 'runs': total, 'repeats': repeats,
        'success_rate': round(success / total, 4) if total else 0.0,
        'task_success_rate': round(success / total, 4) if total else 0.0,
        'pass_k_rate': round(pass_k, 4), 'pass_k_samples': len(grouped),
        'hard_constraint_rate': round(hard, 4), 'hard_constraint_samples': hard_n,
        'no_fabrication_rate': round(fabrication, 4), 'no_fabrication_samples': len(fabrication_rows),
        'memory_op_accuracy': round(memory, 4), 'memory_op_samples': memory_n,
        'memory_faithfulness_rate': round(memory_faith, 4), 'memory_faithfulness_samples': memory_faith_n,
        'grounding_rate': round(grounding, 4), 'grounding_samples': grounding_n,
        'judge_faithfulness_rate': judge_mean('faithfulness'), 'judge_answer_relevancy_rate': judge_mean('answer_relevancy'),
        'judge_context_precision': judge_mean('context_precision'), 'judge_context_recall': judge_mean('context_recall'),
        'judge_memory_faithfulness_rate': judge_mean('memory_faithfulness'), 'judge_samples': len(judge_rows),
        'safety_rate': round(safety, 4), 'safety_samples': len(safety_rows),
        'isolation_rate': round(isolation, 4), 'isolation_samples': isolation_n,
        'authority_refusal_rate': round(authority, 4), 'authority_samples': authority_n,
        'forbidden_tool_rate': round(forbidden, 4), 'forbidden_samples': forbidden_n,
        'redundant_call_rate': round(redundant / tool_total, 4), 'tool_error_rate': round(tool_errors / tool_total, 4),
        'degrade_rate': round(degrade, 4), 'degrade_samples': degrade_n,
        'rag_evidence_rate': round(evidence, 4), 'rag_evidence_samples': evidence_n,
        'gate_rate': round(gate, 4), 'gate_samples': gate_n,
        'trajectory_rate': round(plan, 4), 'trajectory_samples': plan_n,
        'plan_rate': round(plan, 4), 'plan_samples': plan_n,
        'efficiency': {
            'mean_duration_seconds': round(sum(durations) / len(durations), 3) if durations else 0.0,
            'p95_duration_seconds': round(p95(durations), 3),
            'mean_tool_calls': round(sum(tool_counts) / len(tool_counts), 2) if tool_counts else 0.0,
            'p95_tool_calls': p95(tool_counts),
            'mean_model_calls': round(sum(model_calls) / len(model_calls), 2) if model_calls else 0.0,
            'mean_tokens': round(sum(tokens) / len(tokens), 1) if tokens else 0.0,
            'p95_tokens': p95(tokens),
            'reused_call_rate': round(reused / tool_total, 4),
            'timeout_rate': round(timeouts / tool_total, 4),
            'retry_rate': round(retries / tool_total, 4),
        },
        'per_polarity': {name: {**values, 'accuracy': round(values['passed'] / values['total'], 4)}
                         for name, values in sorted(per_polarity.items())},
        'per_category': {name: {**values, 'accuracy': round(values['passed'] / values['total'], 4)}
                         for name, values in sorted(per_category.items())},
        'failure_reasons': dict(sorted(failures.items(), key=lambda item: -item[1])),
    }


FATAL_MARKERS = ('insufficient balance', 'error code: 402', 'error code: 401', 'invalid api key', 'unauthorized')


class FatalModelError(RuntimeError):
    """The account cannot serve requests; continuing would burn the whole run."""


def _fatal(message: str | None) -> bool:
    text = (message or '').lower()
    return any(marker in text for marker in FATAL_MARKERS)


async def _run_capability(args) -> dict:
    samples = build_scenarios(args.per_category)
    if args.limit:
        samples = _round_robin(samples, args.limit)
    repeats=max(1,args.repeats)
    workers = max(1, min(args.workers, len(samples)))
    checkpoint = Path(args.checkpoint) if args.checkpoint else (Path(str(args.out) + '.jsonl') if args.out else None)
    existing = []
    if checkpoint and checkpoint.exists() and not args.fresh:
        for line in checkpoint.read_text('utf-8').splitlines():
            try:existing.append(json.loads(line))
            except ValueError:continue
    elif checkpoint and args.fresh and checkpoint.exists():
        checkpoint.unlink()
    done = {(row.get('id'), int(row.get('repeat', 0))) for row in existing if row.get('id')}
    print(f'capability samples={len(samples)} repeats={repeats} {capability_counts(samples)} mode={args.mode} workers={workers}'
          f' checkpoint={checkpoint or "-"} resumed={len(existing)}', flush=True)
    runtimes = [build_capability_runtime(args.mode) for _ in range(workers)]
    evaluators = [ScenarioEvaluator(runtime, judge=GroundingJudge(runtime.model) if args.judge and args.mode == 'online' else None)
                  for runtime in runtimes]
    shards = [samples[index::workers] for index in range(workers)]
    started = time.time();lock=asyncio.Lock()

    async def append(row):
        if checkpoint is None:return
        checkpoint.parent.mkdir(parents=True,exist_ok=True)
        async with lock:
            with checkpoint.open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(row,ensure_ascii=False)+'\n');stream.flush()

    async def run_shard(evaluator: ScenarioEvaluator, shard: list[dict]) -> list[dict]:
        rows = []
        for sample in shard:
            for repeat in range(repeats):
                if (sample['id'],repeat) in done:continue
                observed = await evaluator.run(sample)
                if _fatal(observed.get('error')):
                    raise FatalModelError(observed['error'][:200])
                scored = score_scenario(sample, observed)
                trace = observed.get('trace') or []
                row={'id': sample['id'], 'repeat': repeat, 'category': sample['category'],
                     'polarity': sample.get('polarity', 'positive'),
                     'text': sample.get('text') or ' / '.join(sample.get('turns', [])) or sample['kind'],
                     'error': observed.get('error'), 'duration_seconds': observed.get('duration_seconds'),
                     'tool_calls': observed.get('tool_calls'),
                     'reused_calls': sum(1 for step in trace if step.get('status') == 'reused'),
                     'tokens': observed.get('tokens'), 'model_calls': observed.get('model_calls'),
                     'timeouts': observed.get('timeouts'), 'retries': observed.get('retries'),
                     'redundant_calls': observed.get('redundant_calls'), 'tool_errors': observed.get('tool_errors'),
                     'report': observed.get('report'), 'trace': trace, 'judge': observed.get('judge'),
                     'judge_error': observed.get('judge_error'), **scored}
                rows.append(row);done.add((sample['id'],repeat));await append(row)
        return rows

    try:
        parts = await asyncio.gather(*(run_shard(evaluator, shard) for evaluator, shard in zip(evaluators, shards)))
    except FatalModelError as error:
        print(f'中止：模型账户不可用（{error}）。已停止，避免继续消耗调用。', flush=True)
        if checkpoint:print(f'已完成结果已保存到 {checkpoint}，补足额度后用同一 --out/--checkpoint 重跑即可跳过已完成样本。', flush=True)
        raise
    finally:
        await asyncio.gather(*(runtime.close() for runtime in runtimes), return_exceptions=True)
    results = existing + [row for part in parts for row in part]
    metrics = _aggregate_capability(results, mode=args.mode, repeats=repeats)
    metrics.update(duration_seconds=round(time.time() - started, 1), workers=workers, repeats=repeats,
                   generated_at=datetime.now(timezone.utc).isoformat(), checkpoint=str(checkpoint) if checkpoint else None,
                   resumed_samples=len(existing),
                   notes='Live model run.' if args.mode == 'online'
                   else 'Scripted pipeline run: host behaviour only, not model quality.')
    return {'metrics': metrics, 'results': results}


def main() -> int:
    parser = argparse.ArgumentParser(description='房产经纪智能助手评测')
    parser.add_argument('--suite', choices=('capability', 'legacy'), default='capability')
    parser.add_argument('--mode', choices=('scripted', 'online'), default='scripted')
    parser.add_argument('--per-category', type=int, default=100)
    parser.add_argument('--limit', type=int, default=0, help='只跑前 N 条，用于冒烟')
    parser.add_argument('--workers', type=int, default=1, help='并发工作器数量（每个工作器独立数据目录）')
    parser.add_argument('--repeats', type=int, default=1, help='同一样本重复次数；>1 时输出 pass^k')
    parser.add_argument('--judge', action='store_true', help='启用 LLM Judge 计算 RAGAS 风格语义指标（仅 online）')
    parser.add_argument('--out', default='', help='指标 JSON 输出路径')
    parser.add_argument('--checkpoint', default='', help='逐条 JSONL 检查点路径；默认 <out>.jsonl')
    parser.add_argument('--fresh', action='store_true', help='忽略已有检查点，从头跑')
    args = parser.parse_args()
    if args.mode == 'online':
        from ..config import load_settings

        settings = load_settings(create_data_dir=False)
        if not (settings.chat_api_key and settings.chat_model):
            print('online 模式需要在 .env 中配置 OPENAI_API_KEY 与 PROPERTY_MODEL；未配置时请使用 --mode scripted。', flush=True)
            return 2
    runner = _run_capability if args.suite == 'capability' else _run_legacy
    try:
        payload = asyncio.run(runner(args))
    except FatalModelError:
        return 3
    metrics = payload['metrics']
    keys = ('samples', 'task_success_rate', 'hard_constraint_rate', 'no_fabrication_rate', 'grounding_rate',
            'memory_faithfulness_rate', 'memory_op_accuracy', 'safety_rate', 'isolation_rate', 'degrade_rate',
            'rag_evidence_rate', 'gate_rate', 'trajectory_rate', 'plan_rate', 'judge_faithfulness_rate',
            'duration_seconds', 'resumed_samples')
    print(json.dumps({key: metrics[key] for key in keys if key in metrics}, ensure_ascii=False, indent=2))
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        print('written', path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
