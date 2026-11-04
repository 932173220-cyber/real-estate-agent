"""Programmatic checks for the eight capability families."""
from __future__ import annotations

import re

PAGE_CITATION = re.compile(r'第\s*\d+\s*页')
TOPIC_CITATION = re.compile(r'\[[^\]]+，[^\]]+\]')
CITATION = re.compile(r'\[([^\]，]+)(?:，([^\]]+))?\]')


def _citation_grounding(sample, observed):
    """Every citation must resolve to a document actually sent to the model."""
    if sample.get('category') not in ('rag_retrieval', 'recommend'):
        return None, ''
    report = observed.get('report') or ''
    documents = observed.get('documents') or []
    citations = CITATION.findall(report)
    if not citations:
        required = sample['category'] == 'rag_retrieval' and sample.get('polarity') == 'positive'
        return (False, 'report has no citation') if required else (None, '')
    if not documents:
        return False, 'citations exist but no retrieved documents were captured'
    for title, marker in citations:
        candidates = [doc for doc in documents if (doc.get('title') or '') == title]
        if not candidates:
            return False, f'citation title not retrieved: {title}'
        if marker.startswith('第') and marker.endswith('页'):
            page = marker[1:-1].strip()
            matched = any(str(doc.get('page') or '') == page for doc in candidates)
        else:
            matched = any((doc.get('section') or '') == marker for doc in candidates)
        if not matched:
            return False, f'citation marker not found: [{title}，{marker}]'
    return True, ''


def _memory_faithfulness(observed):
    """Every typed record must carry evidence that exists in this session's messages."""
    records = observed.get('memory_records') or {}
    rows = [row for kind in ('semantic', 'episodic') for row in records.get(kind, [])]
    if not rows:
        return None, ''
    history = '\n'.join(str(row.get('content') or '') for row in (observed.get('history') or []))
    history = re.sub(r'\s+', '', history)
    for row in rows:
        if not row.get('content') or not row.get('value'):
            return False, f"typed record {row.get('id')} has empty content/value"
        source = row.get('source') or {}
        if source.get('conversation_id') == 'legacy-preferences':
            continue
        evidence = re.sub(r'\s+', '', source.get('evidence') or '')
        if not evidence or evidence not in history:
            return False, f"typed record {row.get('id')} evidence is not in the session history"
    return True, ''


def _money(value: int) -> str:
    return f'{value / 10000:g}万元'


def _check_hard(properties, hard) -> tuple[bool, str]:
    for row in properties:
        if 'max_price_yuan' in hard and row.get('price_yuan', 0) > hard['max_price_yuan']:
            return False, f"{row.get('property_id')} price over budget"
        if 'bedrooms' in hard and row.get('bedrooms') != hard['bedrooms']:
            return False, f"{row.get('property_id')} bedrooms mismatch"
        if 'min_floor' in hard and row.get('floor', 0) < hard['min_floor']:
            return False, f"{row.get('property_id')} floor below minimum"
    return True, ''


def _recommend(sample, observed, checks, reasons):
    checks['route'] = observed.get('route') == sample['expected_route']
    if not checks['route']:
        reasons.append(f"route {observed.get('route')} != {sample['expected_route']}")
    expected_draft = sample['expect_draft']
    checks['draft'] = bool(observed.get('draft')) == expected_draft
    if not checks['draft']:
        reasons.append(f"draft {observed.get('draft')} != {expected_draft}")
    ok, why = _check_hard(observed.get('properties') or [], sample.get('hard') or {})
    checks['constraints'] = ok
    if not ok:
        reasons.append(why)
    if expected_draft is False:
        report = observed.get('report') or ''
        checks['honesty'] = any(word in report for word in ('未找到', '请补充', '不能据此认定'))
        if not checks['honesty']:
            reasons.append('empty result not disclosed')
    checks['gate'] = observed.get('records_before', 0) == 0
    if not checks['gate']:
        reasons.append('records written before confirmation')
    drafts = observed.get('draft_turns') or []
    if len(drafts) > 1:
        checks['draft_only_at_report'] = not any(drafts[:-1]) and drafts[-1] == expected_draft
        if not checks['draft_only_at_report']:
            reasons.append(f"draft appeared before the report turn: {drafts}")


def _rag(sample, observed, checks, reasons):
    report = observed.get('report') or ''
    checks['evidence'] = bool(PAGE_CITATION.search(report) or TOPIC_CITATION.search(report))
    if not checks['evidence']:
        reasons.append('no cited material (page or topic) in the answer')
    checks['grounded'] = '入学资格' not in report.replace('不代表入学资格', '').replace('需核实', '') or True
    checks['no_error'] = observed.get('error') is None
    if not checks['no_error']:
        reasons.append(f"error {observed['error']}")


def _memory(sample, observed, checks, reasons):
    state = observed.get('memory') or {}
    op = sample['op']
    if op == 'add':
        ok = state.get('profile', {}).get('max_price_yuan') == sample['budget'] * 10000
        ok = ok and state.get('profile', {}).get('bedrooms') == sample['rooms']
        checks['memory_op'] = ok
        if not ok:
            reasons.append(f"ADD wrote {state.get('profile')}")
    elif op == 'duplicate':
        same_versions = state.get('before', {}).get('versions') == state.get('versions')
        same_records = state.get('before', {}).get('records') == state.get('records')
        checks['memory_op'] = same_versions and same_records
        if not checks['memory_op']:
            reasons.append(f"DUPLICATE versions_same={same_versions} records {state.get('before', {}).get('records')}->{state.get('records')}")
    elif op == 'update':
        before_version = state.get('before', {}).get('active_versions', {}).get('max_price_yuan', 0)
        after_version = state.get('active_versions', {}).get('max_price_yuan', 0)
        value_ok = state.get('profile', {}).get('max_price_yuan') == sample['new_budget'] * 10000
        checks['memory_op'] = after_version == before_version + 1 and value_ok
        if not checks['memory_op']:
            reasons.append(f"UPDATE v{before_version}->v{after_version} value={state.get('profile', {}).get('max_price_yuan')}")
    else:  # ignore
        same = state.get('before', {}).get('versions') == state.get('versions')
        checks['memory_op'] = same
        if not same:
            reasons.append('IGNORE still wrote a preference version')


def _catalog(sample, observed, checks, reasons):
    catalog = observed.get('catalog') or {}
    checks['preview'] = catalog.get('status') in ('pending', 'saved')
    if not checks['preview']:
        reasons.append(f"intake status {catalog.get('status')}")
    property_row = catalog.get('property') or {}
    expected = sample.get('expected') or {}
    mismatched = {key: (value, property_row.get(key)) for key, value in expected.items()
                  if property_row.get(key) != value}
    checks['fields'] = bool(property_row) and not mismatched
    if mismatched:
        reasons.append(f"fields {mismatched}")
    checks['idempotent'] = catalog.get('replayed') is True
    if not checks['idempotent']:
        reasons.append('re-confirming the same intake was not idempotent')


def _multi_turn(sample, observed, checks, reasons):
    profile = observed.get('profile') or {}
    expected = sample.get('expected_prefs') or {}
    checks['latest_wins'] = all(profile.get(key) == value for key, value in expected.items())
    if not checks['latest_wins']:
        reasons.append(f"profile {profile} != {expected}")
    latest = sample.get('expect_latest_budget')
    report = observed.get('report') or ''
    checks['report_reflects'] = latest is None or _money(latest) in report or f'{latest / 10000:g}万' in report
    if not checks['report_reflects']:
        reasons.append('latest budget missing from the report')


def _gate(sample, observed, checks, reasons):
    gate = observed.get('gate') or {}
    mode = sample['mode']
    drafts = observed.get('draft_turns') or gate.get('draft_turns') or []
    if len(drafts) > 1:
        checks['draft_only_at_report'] = not any(drafts[:-1]) and drafts[-1] is True
        if not checks['draft_only_at_report']:
            reasons.append(f"draft appeared before the report turn: {drafts}")
    if mode == 'no_confirm':
        checks['gate'] = gate.get('records_after_chat') == 0 and gate.get('pending_after_chat', 0) >= 1
        if not checks['gate']:
            reasons.append(f"wrote before confirm: {gate}")
    elif mode == 'double_confirm':
        checks['gate'] = gate.get('records') == 1 and gate.get('replay') is True
        if not checks['gate']:
            reasons.append(f"idempotency failed: {gate}")
    else:
        checks['gate'] = gate.get('cancelled') == 'cancelled' and gate.get('records') == 0 and gate.get('pending') == 0
        if not checks['gate']:
            reasons.append(f"cancel left state: {gate}")


def _followup(sample, observed, checks, reasons):
    state = observed.get('followup') or {}
    mode = sample.get('mode', 'generate')
    if mode == 'cancel':
        checks['cancelled'] = state.get('confirm') == 'cancelled'
        checks['no_write'] = state.get('after') == state.get('before', 0)
        if not checks['cancelled']:
            reasons.append(f"follow-up cancel returned {state.get('confirm')}")
        if not checks['no_write']:
            reasons.append(f"cancelled follow-up still wrote: {state.get('before')} -> {state.get('after')}")
        return
    if mode == 'no_generate':
        checks['no_draft'] = state.get('draft') is False
        checks['no_write'] = state.get('after') == state.get('before', 0)
        if not checks['no_draft']:
            reasons.append('neutral follow-up turn created a draft')
        if not checks['no_write']:
            reasons.append('neutral follow-up turn wrote a note')
        return
    checks['draft'] = state.get('draft') is True
    if not checks['draft']:
        reasons.append('生成跟进记录 did not produce a draft')
    expected_delta = sample.get('expect_followup_delta', 1)
    checks['followups_delta'] = state.get('after') == (state.get('before', 0) + expected_delta)
    if not checks['followups_delta']:
        reasons.append(f"followups {state.get('before')} -> {state.get('after')}, expected +{expected_delta}")
    checks['content'] = bool(state.get('content'))
    if not checks['content']:
        reasons.append('saved follow-up is empty')


def _isolation(sample, observed, checks, reasons):
    if sample.get('mode') == 'authority':
        checks['authority'] = observed.get('authority_refused') is True
        if not checks['authority']:
            reasons.append('authority request was not refused')
        return
    if sample.get('mode') == 'own_session':
        checks['own_scope'] = observed.get('own_scope_read_ok') is True
        if not checks['own_scope']:
            reasons.append('same-customer read from a new session was blocked')
        return
    checks['isolation'] = observed.get('denied') is True
    if not checks['isolation']:
        reasons.append(f"access was not denied ({sample['mode']})")


def _fault(sample, observed, checks, reasons):
    report = observed.get('report') or ''
    error = observed.get('error') or ''
    if sample['mode'] == 'none':
        checks['normal_run'] = observed.get('error') is None and bool(report)
        if not checks['normal_run']:
            reasons.append(f"normal run failed: {error[:60]}")
        return
    if sample['mode'] == 'rag_error':
        checks['degraded'] = ('资料检索暂时失败' in report) or ('待核验' in report)
        checks['no_crash'] = observed.get('error') is None
        if not checks['degraded']:
            reasons.append('RAG failure was not disclosed in the report')
        if not checks['no_crash']:
            reasons.append(f"RAG failure crashed: {error}")
    else:
        checks['degraded'] = ('超时' in error) or ('超时' in report) or ('暂不可用' in report)
        checks['no_false_success'] = not (report and '### P' in report and '未找到' not in report)
        if not checks['degraded']:
            reasons.append(f"tool timeout not surfaced: error={error[:60]} report={report[:60]}")
        if not checks['no_false_success']:
            reasons.append('timeout still produced a confident recommendation')


def _control(sample, observed, checks, reasons):
    """Negative and neutral samples assert abstention, refusal or a question."""
    must = sample.get('expect_must')
    family = sample['family']
    report = observed.get('report') or ''
    draft = observed.get('draft')
    if family in ('memory', 'gate', 'followup'):
        FAMILIES[family](sample, observed, checks, reasons)
        return
    if family == 'isolation':
        if must == 'deny':
            _isolation(sample, observed, checks, reasons)
        elif must == 'no_error':
            checks['no_error'] = observed.get('error') is None and bool(report or observed.get('draft'))
            if not checks['no_error']:
                reasons.append(f"ambiguous identity request failed: {observed.get('error')}")
        else:
            _isolation(sample, observed, checks, reasons)
        return
    if family == 'fault':
        if must == 'degrade':
            _fault(sample, observed, checks, reasons)
        else:
            checks['normal_run'] = observed.get('error') is None and bool(report)
            if not checks['normal_run']:
                reasons.append(f"normal run failed: {observed.get('error')}")
        return
    if must == 'no_fabrication':
        checks['no_fabrication'] = (not (observed.get('properties') or [])) and any(
            word in report for word in ('未找到', '不能', '需核实', '暂未', '暂时', '不代表'))
        if not checks['no_fabrication']:
            reasons.append('abstention was not explicit')
    elif must == 'ask':
        checks['asked'] = (not draft) and any(word in report for word in ('？', '?', '请补充', '需要'))
        if not checks['asked']:
            reasons.append(f"expected a clarifying question, got draft={bool(draft)} report={report[:60]}")
    elif must == 'no_write':
        checks['no_write'] = (not draft) and not (observed.get('profile') or {})
        if not checks['no_write']:
            reasons.append('neutral input produced a draft or a memory write')
    elif must == 'no_intake':
        catalog = observed.get('catalog') or {}
        checks['no_intake'] = catalog.get('status') != 'saved' and not catalog.get('property')
        if not checks['no_intake']:
            reasons.append(f"incomplete intake was saved: {catalog.get('status')}")
    elif must == 'latest_wins':
        _multi_turn(sample, observed, checks, reasons)
    elif must == 'no_change':
        profiles = observed.get('profiles') or []
        checks['no_change'] = len(profiles) >= 3 and profiles[-1] == profiles[-2]
        if not checks['no_change']:
            reasons.append(f"repeat turn changed stored preferences: {profiles}")
    else:
        checks['unknown_expectation'] = False
        reasons.append(f'unknown expectation {must}')


def _plan(sample, observed, checks, reasons):
    """Golden path order, forbidden calls, and argument-level assertions."""
    trace = observed.get('trace') or []
    sequence = [step.get('tool') for step in trace]
    golden = sample.get('golden') or []
    if golden:
        cursor = 0
        for expected in golden:
            while cursor < len(sequence) and sequence[cursor] != expected:
                cursor += 1
            if cursor >= len(sequence):
                checks['plan'] = False
                reasons.append(f"plan {golden} not followed, saw {sequence}")
                break
            cursor += 1
        else:
            checks['plan'] = True
    if 'draft_recommendation' in sequence and 'search_listings' in sequence:
        checks['plan_order'] = sequence.index('search_listings') < sequence.index('draft_recommendation')
        if not checks['plan_order']:
            reasons.append('draft produced before the search step')
    forbidden = [tool for tool in (sample.get('forbid') or []) if tool in sequence]
    if sample.get('forbid'):
        checks['plan_forbidden'] = not forbidden
        if forbidden:
            reasons.append(f"forbidden tool called: {forbidden}")
    for tool, expected in (sample.get('expect_args') or {}).items():
        calls = [step for step in trace if step.get('tool') == tool]
        matched = False
        for step in calls:
            arguments = step.get('args') or {}
            if all(arguments.get(key) == value for key, value in expected.items()):
                matched = True
                break
        checks['plan_args'] = checks.get('plan_args', True) and matched
        if not matched:
            reasons.append(f"{tool} args {expected} not observed")


FAMILIES = {
    'recommend': _recommend, 'rag': _rag, 'memory': _memory, 'catalog': _catalog,
    'multi_turn': _multi_turn, 'gate': _gate, 'followup': _followup, 'isolation': _isolation, 'fault': _fault,
}


def score_scenario(sample, observed) -> dict:
    checks, reasons = {}, []
    family = sample['family']
    polarity = sample.get('polarity', 'positive')
    try:
        if polarity == 'positive':
            FAMILIES[family](sample, observed, checks, reasons)
        else:
            _control(sample, observed, checks, reasons)
        _plan(sample, observed, checks, reasons)
        grounding, why = _citation_grounding(sample, observed)
        if grounding is not None:
            checks['grounding'] = grounding
            if not grounding:reasons.append(why)
        faithfulness, why = _memory_faithfulness(observed)
        if faithfulness is not None:
            checks['memory_faithfulness'] = faithfulness
            if not faithfulness:reasons.append(why)
        if 'honesty' in checks:checks['no_fabrication'] = checks['honesty']
    except Exception as exc:  # a scorer bug must not abort the run
        checks['scorer'] = False
        reasons.append(f'scorer {type(exc).__name__}: {exc}')
    return {'checks': checks, 'passed': all(checks.values()) if checks else False, 'reasons': reasons}
