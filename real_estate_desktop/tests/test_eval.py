import unittest

from real_estate_desktop.eval.dataset import CATEGORIES, build_samples, summary_counts
from real_estate_desktop.eval.runner import aggregate
from real_estate_desktop.eval.scorers import score_sample


class Dataset(unittest.TestCase):
    def test_eight_families_of_one_hundred(self):
        samples = build_samples()
        self.assertEqual(len(samples), 800)
        counts = summary_counts(samples)
        self.assertEqual(set(counts), set(CATEGORIES))
        self.assertTrue(all(value == 100 for value in counts.values()))

    def test_samples_are_deterministic(self):
        first = {sample['id']: sample['text'] for sample in build_samples()}
        second = {sample['id']: sample['text'] for sample in build_samples()}
        self.assertEqual(first, second)

    def test_each_family_expects_something_checkable(self):
        for sample in build_samples(per_category=2):
            if sample['category'] in ('budget_only', 'parallel'):
                self.assertIn('hard', sample)
            if sample['category'] == 'memory_update':
                self.assertTrue(sample['expected_prefs'])


class Scorers(unittest.TestCase):
    def sample(self, **overrides):
        base = {'id': 'x-000', 'category': 'budget_only', 'route': 'listing_first', 'draft': True,
                'hard': {'max_price_yuan': 3000000}, 'expected_prefs': {'max_price_yuan': 3000000, 'bedrooms': 3},
                'expect_followup': None, 'check_isolation': True}
        base.update(overrides)
        return base

    def observed(self, **overrides):
        base = {'route': 'listing_first', 'draft': True, 'properties': [{'property_id': 'P001', 'price_yuan': 2900000, 'bedrooms': 3, 'floor': 8}],
                'report': '报告', 'profile': {'max_price_yuan': 3000000, 'bedrooms': 3}, 'followups': [],
                'records_before': 0, 'isolation_blocked': True}
        base.update(overrides)
        return base

    def test_clean_sample_passes_every_check(self):
        result = score_sample(self.sample(), self.observed())
        self.assertTrue(result['passed'], result['reasons'])
        self.assertEqual(set(result['checks']), {'route', 'draft', 'constraints', 'honesty', 'memory', 'isolation', 'gate'})

    def test_over_budget_property_fails_the_hard_constraint(self):
        result = score_sample(self.sample(), self.observed(properties=[{'property_id': 'P013', 'price_yuan': 4680000}]))
        self.assertFalse(result['passed'])
        self.assertFalse(result['checks']['constraints'])

    def test_write_before_confirmation_fails_the_gate(self):
        result = score_sample(self.sample(), self.observed(records_before=1))
        self.assertFalse(result['checks']['gate'])

    def test_cross_customer_leak_fails_isolation(self):
        result = score_sample(self.sample(), self.observed(isolation_blocked=False))
        self.assertFalse(result['checks']['isolation'])

    def test_a_wrong_secondary_field_fails_the_memory_check(self):
        result = score_sample(self.sample(), self.observed(profile={'max_price_yuan': 3000000, 'bedrooms': 2}))
        self.assertFalse(result['checks']['memory'])
        self.assertIn('bedrooms', ' '.join(result['reasons']))

    def test_empty_candidates_without_disclosure_fails_honesty(self):
        result = score_sample(self.sample(), self.observed(properties=[]))
        self.assertFalse(result['checks']['honesty'])
        good = score_sample(self.sample(), self.observed(properties=[], report='未找到满足条件的候选'))
        self.assertTrue(good['checks']['honesty'])


class Aggregation(unittest.TestCase):
    def test_metrics_are_rates_over_applicable_samples(self):
        results = [
            {'category': 'budget_only', 'passed': True, 'checks': {'constraints': True, 'memory': True}, 'reasons': []},
            {'category': 'budget_only', 'passed': False, 'checks': {'constraints': False, 'memory': True}, 'reasons': ['constraints: over budget']},
        ]
        metrics = aggregate(results, mode='scripted')
        self.assertEqual(metrics['samples'], 2)
        self.assertEqual(metrics['success_rate'], 0.5)
        self.assertEqual(metrics['hard_constraint_rate'], 0.5)
        self.assertEqual(metrics['memory_accuracy'], 1.0)
        self.assertEqual(metrics['per_category']['budget_only']['accuracy'], 0.5)
        self.assertEqual(metrics['failure_reasons'], {'constraints': 1})


class FatalAccount(unittest.TestCase):
    def test_account_errors_are_recognised(self):
        from real_estate_desktop.eval.run import _fatal

        self.assertTrue(_fatal("APIStatusError: Error code: 402 - Insufficient Balance"))
        self.assertTrue(_fatal('APIStatusError: Error code: 401 - invalid api key'))
        self.assertFalse(_fatal('ValueError: 模型未返回可校验的检索计划'))
        self.assertFalse(_fatal(None))


class Isolation(unittest.TestCase):
    def test_online_runtime_never_uses_the_application_data_dir(self):
        import asyncio
        from pathlib import Path
        from unittest.mock import patch

        from real_estate_desktop.config import Settings
        from real_estate_desktop.eval.runner import build_runtime

        async def scenario():
            fake = Settings(Path('D:/not-the-app'), chat_api_key='x', chat_model='m')
            with patch('real_estate_desktop.config.load_settings', return_value=fake):
                runtime = build_runtime('online')
            try:
                return runtime.settings.root
            finally:
                await runtime.close()

        root = asyncio.run(scenario())
        self.assertNotEqual(str(root), 'D:\\not-the-app')
        self.assertIn('pa-eval-', str(root))


if __name__ == '__main__':
    unittest.main()
