import unittest

from real_estate_desktop.eval.scenario_scorers import _citation_grounding, _memory_faithfulness, score_scenario
from real_estate_desktop.eval.scenarios import CATEGORIES, build_scenarios, summary_counts


class Scenarios(unittest.TestCase):
    def test_nine_capability_families_of_one_hundred(self):
        samples = build_scenarios()
        self.assertEqual(len(samples), 900)
        counts = summary_counts(samples)
        self.assertEqual(set(counts), set(CATEGORIES))
        self.assertTrue(all(value == 100 for value in counts.values()))
        follow = [row['polarity'] for row in samples if row['category'] == 'followup']
        self.assertEqual({name: follow.count(name) for name in ('positive', 'negative', 'neutral')},
                         {'positive': 60, 'negative': 20, 'neutral': 20})
        auto = [row for row in samples if row.get('auto_memory')]
        self.assertEqual(len(auto), 12)
        self.assertEqual({row['op'] for row in auto}, {'add', 'update'})
        authority = [row for row in samples if row['category'] == 'isolation' and row.get('mode') == 'authority']
        self.assertEqual(len(authority), 10)

    def test_ids_are_unique_and_stable(self):
        first = [sample['id'] for sample in build_scenarios()]
        second = [sample['id'] for sample in build_scenarios()]
        self.assertEqual(first, second)
        self.assertEqual(len(set(first)), len(first))

    def test_every_sample_declares_its_family(self):
        for sample in build_scenarios(per_category=1):
            self.assertIn(sample['family'], ('recommend', 'rag', 'memory', 'catalog',
                                             'multi_turn', 'gate', 'followup', 'isolation', 'fault'))


class PlanCheck(unittest.TestCase):
    def sample(self, golden):
        return {'family': 'recommend', 'category': 'recommend', 'golden': golden,
                'expected_route': 'listing_first', 'expect_draft': True,
                'hard': {'max_price_yuan': 4000000}, 'checks_isolation': False}

    def observed(self, tools):
        return {'route': 'listing_first', 'draft': True, 'properties': [], 'report': 'x',
                'records_before': 0, 'trace': [{'tool': name, 'status': 'ok'} for name in tools]}

    def test_golden_path_in_order_passes(self):
        result = score_scenario(self.sample(['search_listings', 'draft_recommendation']),
                                self.observed(['query_customer_memory', 'search_listings', 'draft_recommendation']))
        self.assertTrue(result['checks']['plan'])

    def test_skipped_step_fails(self):
        result = score_scenario(self.sample(['search_listings', 'retrieve_area_info', 'draft_recommendation']),
                                self.observed(['search_listings', 'draft_recommendation']))
        self.assertFalse(result['checks']['plan'])
        self.assertIn('plan', ' '.join(result['reasons']))

    def test_out_of_order_steps_fail(self):
        result = score_scenario(self.sample(['search_listings', 'draft_recommendation']),
                                self.observed(['draft_recommendation', 'search_listings']))
        self.assertFalse(result['checks']['plan'])

    def test_draft_before_search_is_flagged(self):
        result = score_scenario({'family': 'recommend', 'category': 'recommend', 'golden': [],
                                 'expected_route': 'listing_first', 'expect_draft': True,
                                 'hard': {}, 'checks_isolation': False},
                                self.observed(['draft_recommendation', 'search_listings']))
        self.assertFalse(result['checks'].get('plan_order', False))


class SafetyScoring(unittest.TestCase):
    def test_authority_refusal_is_a_safety_check(self):
        sample = {'family': 'isolation', 'category': 'isolation', 'polarity': 'negative', 'mode': 'authority'}
        self.assertTrue(score_scenario(sample, {'authority_refused': True})['checks']['authority'])
        self.assertFalse(score_scenario(sample, {'authority_refused': False})['checks']['authority'])


class GroundingAndMemory(unittest.TestCase):
    def test_citation_must_resolve_to_a_retrieved_document(self):
        sample = {'family': 'rag', 'category': 'rag_retrieval', 'polarity': 'positive', 'golden': []}
        good = {'report': '[翠竹苑，交通] 步行约十分钟。', 'documents': [{'title': '翠竹苑', 'section': '交通', 'page': None}], 'error': None}
        self.assertTrue(score_scenario(sample, good)['checks']['grounding'])
        bad = {'report': '[翠竹苑，交通] 步行约十分钟。', 'documents': [{'title': '云庭花园', 'section': '交通', 'page': None}], 'error': None}
        self.assertFalse(score_scenario(sample, bad)['checks']['grounding'])

    def test_followup_citations_do_not_require_rag_documents(self):
        sample = {'category': 'followup', 'polarity': 'positive'}
        self.assertEqual(_citation_grounding(sample, {'report': '[某小区，交通] 步行十分钟。'}), (None, ''))

    def test_memory_faithfulness_requires_evidence_in_the_session(self):
        observed = {'memory_records': {'semantic': [
            {'id': 'm1', 'content': '客户已经退休', 'value': '退休',
             'source': {'conversation_id': 's1', 'evidence': '客户已经退休'}}]},
            'history': [{'role': 'user', 'content': '客户已经退休，想看房'}]}
        self.assertTrue(_memory_faithfulness(observed)[0])
        observed['memory_records']['semantic'][0]['source']['evidence'] = '客户没有工作'
        self.assertFalse(_memory_faithfulness(observed)[0])


class FollowUpScoring(unittest.TestCase):
    def test_one_new_saved_note_is_required(self):
        sample = {'family': 'followup', 'category': 'followup', 'polarity': 'positive', 'expect_followup_delta': 1}
        good = score_scenario(sample, {'followup': {'draft': True, 'before': 1, 'after': 2, 'content': '本次沟通…'}})
        self.assertTrue(good['passed'])
        missing = score_scenario(sample, {'followup': {'draft': False, 'before': 1, 'after': 1, 'content': ''}})
        self.assertFalse(missing['passed'])
    def test_cancel_and_no_generate_do_not_write(self):
        cancel = {'family': 'followup', 'category': 'followup', 'polarity': 'negative', 'mode': 'cancel'}
        self.assertTrue(score_scenario(cancel, {'followup': {'draft': True, 'before': 1, 'after': 1, 'confirm': 'cancelled'}})['passed'])
        neutral = {'family': 'followup', 'category': 'followup', 'polarity': 'neutral', 'mode': 'no_generate'}
        self.assertTrue(score_scenario(neutral, {'followup': {'draft': False, 'before': 0, 'after': 0}})['passed'])


class FaultScoring(unittest.TestCase):
    def test_rag_failure_needs_an_explicit_disclosure(self):
        sample = {'family': 'fault', 'category': 'fault_handling', 'mode': 'rag_error'}
        quiet = score_scenario(sample, {'report': '已为您推荐 3 套房源。', 'error': None})
        self.assertFalse(quiet['checks']['degraded'])
        honest = score_scenario(sample, {'report': '小区资料检索暂时失败，本次保留房源硬信息。', 'error': None})
        self.assertTrue(honest['checks']['degraded'])

    def test_timeout_must_be_surfaced(self):
        sample = {'family': 'fault', 'category': 'fault_handling', 'mode': 'tool_timeout'}
        silent = score_scenario(sample, {'report': '', 'error': None})
        self.assertFalse(silent['checks']['degraded'])
        surfaced = score_scenario(sample, {'report': '', 'error': 'ValueError: 该项读取超时'})
        self.assertTrue(surfaced['checks']['degraded'])


if __name__ == '__main__':
    unittest.main()
