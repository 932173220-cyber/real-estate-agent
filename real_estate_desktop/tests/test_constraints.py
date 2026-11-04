import unittest

from real_estate_desktop.eval.scenarios import build_scenarios  # noqa: F401  (import keeps package importable)
from real_estate_desktop.search_planning import ConstraintProposal
from real_estate_desktop.workflow import apply_proposals, constraint_value


class Conversion(unittest.TestCase):
    def test_price_units(self):
        self.assertEqual(constraint_value('max_price_yuan', '450万'), 4500000)
        self.assertEqual(constraint_value('max_price_yuan', '450万元'), 4500000)
        self.assertEqual(constraint_value('max_price_yuan', '四百万'), 4000000)
        self.assertEqual(constraint_value('max_price_yuan', '3800000元'), 3800000)

    def test_counts_and_areas(self):
        self.assertEqual(constraint_value('bedrooms', '三室'), 3)
        self.assertEqual(constraint_value('min_floor', '2'), 2)
        self.assertEqual(constraint_value('min_area_sqm', '88.5㎡'), 88.5)

    def test_flags_and_text(self):
        self.assertTrue(constraint_value('require_elevator', '有电梯'))
        self.assertEqual(constraint_value('orientation', '南北'), '南北')

    def test_unparsable_value_raises(self):
        with self.assertRaises(Exception):
            constraint_value('max_price_yuan', '看情况')


class Proposals(unittest.TestCase):
    def proposal(self, field, value, quote, inferred=False):
        return ConstraintProposal(field=field, value=value, quote=quote, inferred=inferred)

    def test_quoted_constraint_is_adopted(self):
        adopted, questions = apply_proposals('想要两房，南北通透', [self.proposal('bedrooms', '2', '两房')], {})
        self.assertEqual(adopted, {'bedrooms': 2})
        self.assertEqual(questions, [])

    def test_unquoted_constraint_is_ignored(self):
        adopted, questions = apply_proposals('预算四百万', [self.proposal('bedrooms', '3', '三室')], {})
        self.assertEqual(adopted, {})
        self.assertEqual(questions, [])

    def test_inferred_constraint_becomes_a_question(self):
        adopted, questions = apply_proposals(
            '首付150万，贷款300万', [self.proposal('max_price_yuan', '450万', '首付150万，贷款300万', inferred=True)], {})
        self.assertEqual(adopted, {})
        self.assertEqual(len(questions), 1)
        self.assertIn('对吗', questions[0])

    def test_conflicting_value_asks_instead_of_overwriting(self):
        adopted, questions = apply_proposals('预算400万', [self.proposal('max_price_yuan', '450万', '预算400万')],
                                             {'max_price_yuan': 4000000})
        self.assertEqual(adopted, {})
        self.assertEqual(len(questions), 1)


if __name__ == '__main__':
    unittest.main()
