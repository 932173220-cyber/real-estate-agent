"""Optional LLM judge for semantic quality metrics.

The judge is deliberately separate from the host's programmatic checks: task
success, hard constraints, gate and isolation never depend on another model
call. Use ``--judge`` only when you want RAGAS-style numbers.
"""
from __future__ import annotations

JUDGE_PROMPT = '''你是独立评测员，不是房产助手。只根据给出的数据打分，不使用外部知识，不替助手补全事实。
输入：
- question：用户问题或本轮需求。
- report：助手给出的报告/回答。
- documents：本次实际检索到的资料（title/section/page/text）。
- memory_records：本会话写入的语义/情景记忆。
- history：本会话用户与助手的消息。
返回 JSON：
- faithfulness：0~1，report 中的事实性结论是否被 documents 支持；没有依据的结论扣分。
- answer_relevancy：0~1，report 是否回答了 question，而不是泛泛而谈。
- context_precision：0~1，documents 中有多少与 question 相关。
- context_recall：0~1，回答 question 所需的关键资料是否被 documents 覆盖。
- memory_faithfulness：0~1，memory_records 是否被 history 原文支持，是否出现语义反转/臆造；没有记忆时返回 1。
- unsupported_claims：report 中找不到资料依据的结论列表，逐条摘录。
- hallucinated_memories：与 history 不符或原文没有的记忆列表。
- reason：一句话说明扣分原因。
没有对应内容时，该项返回 1；不要因为资料少就臆造支持。'''

JUDGE_SCHEMA = {
    'type': 'object',
    'properties': {
        'faithfulness': {'type': 'number'},
        'answer_relevancy': {'type': 'number'},
        'context_precision': {'type': 'number'},
        'context_recall': {'type': 'number'},
        'memory_faithfulness': {'type': 'number'},
        'unsupported_claims': {'type': 'array', 'items': {'type': 'string'}},
        'hallucinated_memories': {'type': 'array', 'items': {'type': 'string'}},
        'reason': {'type': 'string'},
    },
    'required': ['faithfulness', 'answer_relevancy', 'context_precision', 'context_recall', 'memory_faithfulness'],
}

def _clamp(value):
    try:return max(0.0,min(1.0,float(value)))
    except (TypeError,ValueError):return 0.0

class GroundingJudge:
    def __init__(self, model):
        self.model = model

    async def score(self, sample, observed):
        if self.model is None or not hasattr(self.model, 'memory_json'):
            return None
        documents = observed.get('documents') or []
        data = {
            'question': sample.get('text') or ' / '.join(sample.get('turns', [])),
            'report': (observed.get('report') or '')[:6000],
            'documents': [{key: doc.get(key) for key in ('title', 'section', 'page', 'text')} for doc in documents[:12]],
            'memory_records': {
                kind: [{'content': row.get('content'), 'value': row.get('value'),
                        'evidence': (row.get('source') or {}).get('evidence')}
                       for row in (observed.get('memory_records') or {}).get(kind, [])]
                for kind in ('semantic', 'episodic')
            },
            'history': [{'role': row.get('role'), 'content': row.get('content')} for row in (observed.get('history') or [])][-20:],
        }
        raw = await self.model.memory_json(JUDGE_PROMPT, data, JUDGE_SCHEMA)
        if not isinstance(raw, dict):
            return None
        return {
            'faithfulness': _clamp(raw.get('faithfulness')),
            'answer_relevancy': _clamp(raw.get('answer_relevancy')),
            'context_precision': _clamp(raw.get('context_precision')),
            'context_recall': _clamp(raw.get('context_recall')),
            'memory_faithfulness': _clamp(raw.get('memory_faithfulness')),
            'unsupported_claims': [str(item)[:200] for item in (raw.get('unsupported_claims') or [])][:10],
            'hallucinated_memories': [str(item)[:200] for item in (raw.get('hallucinated_memories') or [])][:10],
            'reason': str(raw.get('reason') or '')[:300],
        }
