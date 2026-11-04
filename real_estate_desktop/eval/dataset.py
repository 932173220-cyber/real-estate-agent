"""Eight task families, 100 deterministic samples each (800 total).

Every sample is generated from the shipped fixture inventory (six communities,
sixteen listings) so expectations can be checked against the database instead
of a hand-written transcript. The generator is seeded, so sample #37 of
``parallel`` is the same text on every machine.
"""
from __future__ import annotations

import random

from ..demo import property_records

CATEGORIES = (
    'budget_only', 'amenity_only', 'parallel', 'property_details',
    'community_listings', 'follow_up', 'memory_update', 'clarify_no_save',
)

COMMUNITIES = ('澄湾雅苑', '云庭花园', '云岸新城', '栖庭里', '松屿公馆', '和溪花园')
LISTINGS = tuple(f'P{index:03d}' for index in range(1, 17))
BUDGETS = (250, 300, 320, 350, 380, 400, 420, 450)
ROOMS = (2, 3, 4)
AMENITIES = (
    ('小学', '希望附近有小学，接送方便'),
    ('中学', '看重中学配套，最好步行能到'),
    ('医院', '希望附近有医院，看病方便'),
    ('超市', '买菜方便，最好有超市'),
    ('公园', '希望附近有公园，可以散步'),
    ('地铁', '希望离地铁近，通勤方便'),
)


def _expected_prefs(**fields):
    return {key: value for key, value in fields.items() if value is not None}


def _budget_only(rng: random.Random, index: int) -> dict:
    budget, rooms = rng.choice(BUDGETS), rng.choice(ROOMS)
    floor = rng.choice(('不要一楼', '楼层不限', '最好中高楼层'))
    text = f'预算{budget}万以内，{rooms}室，{floor}'
    min_floor = 2 if floor == '不要一楼' else None
    # Expectations come from the shipped fixtures, so "draft expected" is only
    # asserted when the database can actually satisfy the request.
    available = [row for row in property_records()
                 if row['price_yuan'] <= budget * 10000 and row['bedrooms'] == rooms
                 and (min_floor is None or row['floor'] >= min_floor)]
    return dict(text=text, route='listing_first', draft=bool(available),
                hard={'max_price_yuan': budget * 10000, 'bedrooms': rooms},
                expected_prefs=_expected_prefs(max_price_yuan=budget * 10000, bedrooms=rooms, min_floor=min_floor)
                if available else {},
                max_price=budget * 10000 if available else None, expect_followup=None)


def _amenity_only(rng: random.Random, index: int) -> dict:
    name, sentence = rng.choice(AMENITIES)
    text = f'{sentence}，先帮我看看有哪些小区'
    return dict(text=text, route='community_first', draft=False, hard={}, max_price=None, expect_followup=None)


def _parallel(rng: random.Random, index: int) -> dict:
    budget, rooms = rng.choice(BUDGETS), rng.choice(ROOMS)
    name, _sentence = rng.choice(AMENITIES)
    hard = {'小学': '附近必须有小学', '中学': '必须附近有中学', '医院': '附近必须有医院',
            '超市': '附近必须有超市', '公园': '附近必须有公园', '地铁': '必须离地铁近'}[name]
    text = f'预算{budget}万以内，{rooms}室，不要一楼，{hard}'
    return dict(text=text, route='parallel', draft=None,
                hard={'max_price_yuan': budget * 10000, 'bedrooms': rooms, 'min_floor': 2},
                max_price=None, expect_followup=None)


def _property_details(rng: random.Random, index: int) -> dict:
    listing = rng.choice(LISTINGS)
    text = f'{listing}这套房子的小区配套怎么样？'
    return dict(text=text, route='property_details', draft=False, hard={}, max_price=None, expect_followup=None)


def _community_listings(rng: random.Random, index: int) -> dict:
    community = rng.choice(COMMUNITIES)
    text = f'看看{community}现在有哪些房源'
    return dict(text=text, route='community_listings', draft=None, hard={}, max_price=None, expect_followup=None)


def _follow_up(rng: random.Random, index: int) -> dict:
    note = rng.choice(('客户表示还想核实通勤时间', '客户约了周末复看两套房源', '客户反馈预算可能需要重新评估'))
    text = f'记录跟进：{note}'
    return dict(text=text, route=None, draft=True, hard={}, max_price=None, expect_followup=note)


def _memory_update(rng: random.Random, index: int) -> dict:
    budget = rng.choice(BUDGETS)
    text = f'只更新偏好：预算{budget}万，三室'
    return dict(text=text, route=None, draft=True, hard={},
                expected_prefs=_expected_prefs(max_price_yuan=budget * 10000, bedrooms=3),
                max_price=budget * 10000, expect_followup=None)


def _clarify_no_save(rng: random.Random, index: int) -> dict:
    text = rng.choice(('帮我随便看看，不保存', '先了解一下，不用保存推荐', '随便推荐几套，只看看'))
    return dict(text=text, route=None, draft=False, hard={}, max_price=None, expect_followup=None)


_BUILDERS = {
    'budget_only': _budget_only,
    'amenity_only': _amenity_only,
    'parallel': _parallel,
    'property_details': _property_details,
    'community_listings': _community_listings,
    'follow_up': _follow_up,
    'memory_update': _memory_update,
    'clarify_no_save': _clarify_no_save,
}


def build_samples(per_category: int = 100, seed: int = 20261031) -> list[dict]:
    samples = []
    for offset, category in enumerate(CATEGORIES):
        rng = random.Random(seed + offset)
        for index in range(per_category):
            body = _BUILDERS[category](rng, index)
            samples.append({
                'id': f'{category}-{index:03d}',
                'category': category,
                'customer': f'评测客户{category}{index}',
                'check_isolation': body['draft'] is not False,
                **body,
            })
    return samples


def summary_counts(samples: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for sample in samples:
        counts[sample['category']] = counts.get(sample['category'], 0) + 1
    return counts
