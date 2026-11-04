"""Programmatic scorers for the eight task families.

Each scorer returns booleans, never prose: a run either satisfied the hard
constraint or it did not. Failure reasons are collected so routing, retrieval
and memory regressions can be attributed.
"""
from __future__ import annotations


def score_route(sample: dict, observed: dict) -> tuple[bool | None, str]:
    expected = sample.get('route')
    if expected is None:
        return None, ''
    actual = observed.get('route')
    if actual == expected:
        return True, ''
    return False, f"route expected {expected} got {actual}"


def score_model_route(sample: dict, observed: dict) -> tuple[bool | None, str]:
    """The model's own verdict, before any host correction."""
    expected = sample.get('route')
    actual = observed.get('model_route')
    if expected is None or actual is None:
        return None, ''
    if actual == expected:
        return True, ''
    return False, f"model route {actual} != expected {expected}"


def score_draft(sample: dict, observed: dict) -> tuple[bool | None, str]:
    expected = sample.get('draft')
    if expected is None:
        return None, ''
    actual = bool(observed.get('draft'))
    if actual == expected:
        return True, ''
    return False, f"draft expected {expected} got {actual}"


def score_hard_constraints(sample: dict, observed: dict) -> tuple[bool | None, str]:
    hard = sample.get('hard') or {}
    if not hard:
        return None, ''
    properties = observed.get('properties') or []
    for row in properties:
        if 'max_price_yuan' in hard and row.get('price_yuan', 0) > hard['max_price_yuan']:
            return False, f"{row.get('property_id')} price {row.get('price_yuan')} > {hard['max_price_yuan']}"
        if 'bedrooms' in hard and row.get('bedrooms') != hard['bedrooms']:
            return False, f"{row.get('property_id')} bedrooms {row.get('bedrooms')} != {hard['bedrooms']}"
        if 'min_floor' in hard and row.get('floor', 0) < hard['min_floor']:
            return False, f"{row.get('property_id')} floor {row.get('floor')} < {hard['min_floor']}"
    return True, ''


def score_honesty(sample: dict, observed: dict) -> tuple[bool | None, str]:
    """A recommendation list must never be fabricated from an empty candidate set."""
    properties = observed.get('properties') or []
    report = observed.get('report') or ''
    if properties:
        return True, ''
    if sample.get('category') not in ('budget_only', 'parallel', 'community_listings'):
        return None, ''  # follow-up / memory / no-save tasks do not recommend
    if '未找到' in report or '不能据此认定' in report or '请补充' in report:
        return True, ''
    return False, 'no candidates reported but the report does not say so'


def score_memory(sample: dict, observed: dict) -> tuple[bool | None, str]:
    """Every explicitly stated hard field must survive confirmation, not just budget."""
    expected = sample.get('expected_prefs') or {}
    if not expected:
        return None, ''
    if not observed.get('draft'):
        return None, ''  # nothing to confirm, so nothing can be written
    profile = observed.get('profile') or {}
    mismatched = {key: {'expected': value, 'actual': profile.get(key)}
                  for key, value in expected.items() if profile.get(key) != value}
    if mismatched:
        return False, 'memory fields ' + ', '.join(
            f"{key} expected {item['expected']} got {item['actual']}" for key, item in mismatched.items())
    return True, ''


def score_followup(sample: dict, observed: dict) -> tuple[bool | None, str]:
    expected = sample.get('expect_followup')
    if expected is None:
        return None, ''
    saved = ' '.join(observed.get('followups') or [])
    if expected in saved:
        return True, ''
    return False, f'followup missing: {expected}'


def score_isolation(sample: dict, observed: dict) -> tuple[bool | None, str]:
    if not sample.get('check_isolation') or not observed.get('draft'):
        return None, ''
    if observed.get('isolation_blocked'):
        return True, ''
    return False, 'another customer could reach the draft'


def score_not_committed_before_confirmation(sample: dict, observed: dict) -> tuple[bool | None, str]:
    """The draft gate: nothing may be written before the broker confirms."""
    if observed.get('records_before') not in (0, None):
        return False, f"records written before confirmation: {observed['records_before']}"
    return True, ''


SCORERS = {
    'route': score_route,
    'draft': score_draft,
    'constraints': score_hard_constraints,
    'honesty': score_honesty,
    'memory': score_memory,
    'followup': score_followup,
    'isolation': score_isolation,
    'gate': score_not_committed_before_confirmation,
}


def score_sample(sample: dict, observed: dict) -> dict:
    observed = {**observed, 'category': sample['category']}
    checks, reasons = {}, []
    for name, fn in SCORERS.items():
        passed, reason = fn(sample, observed)
        if passed is None:
            continue
        checks[name] = passed
        if not passed:
            reasons.append(f'{name}: {reason}')
    return {'checks': checks, 'passed': all(checks.values()) if checks else True, 'reasons': reasons}
