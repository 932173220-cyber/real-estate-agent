import unittest

from real_estate_desktop.config import Settings
from real_estate_desktop.session_memory import SessionMemoryContexts
from real_estate_desktop.workflow import (build_history, compact_candidate, compact_community, compact_document,
                                          compact_memory, compact_plan, compact_tool_output, project_message)


class Projection(unittest.TestCase):
    def test_user_text_is_kept_but_capped(self):
        message = project_message({'role': 'user', 'content': '预' * 900})
        self.assertEqual(message['role'], 'user')
        self.assertEqual(len(message['content']), 400)

    def test_assistant_report_becomes_conclusion_plus_ids(self):
        report = ('# 房源推荐报告\n状态：草稿，尚未确认保存。\n\n## 候选房源\n'
                  '### P001 · 澄湾雅苑\n总价 358万元\n\n客户更关注通勤。')
        message = project_message({'role': 'assistant', 'content': report})
        self.assertNotIn('#', message['content'])
        self.assertNotIn('|', message['content'])
        self.assertIn('P001', message['content'])
        self.assertLessEqual(len(message['content']), 340)

    def test_heading_only_report_still_yields_a_placeholder(self):
        message = project_message({'role': 'assistant', 'content': '# 标题\n## 小标题'})
        self.assertTrue(message['content'])


class HistoryWindow(unittest.TestCase):
    def history(self, turns):
        rows = []
        for index in range(turns):
            rows.append({'role': 'user', 'content': f'需求{index}'})
            rows.append({'role': 'assistant', 'content': f'# 报告{index}\n' + '内容' * 200})
        return rows

    def test_compact_profile_keeps_six_turns_and_shrinks_reports(self):
        settings = Settings.__new__(Settings)
        object.__setattr__(settings, 'history_turns', 6)
        object.__setattr__(settings, 'history_max_tokens', 2000)
        object.__setattr__(settings, 'context_profile', 'compact')
        messages = build_history(self.history(10), settings)
        self.assertLessEqual(len(messages), 12)
        self.assertTrue(all(len(item['content']) <= 400 for item in messages))
        self.assertLessEqual(sum(len(item['content']) for item in messages), 2000)

    def test_legacy_profile_returns_raw_window(self):
        settings = Settings.__new__(Settings)
        object.__setattr__(settings, 'history_turns', 2)
        object.__setattr__(settings, 'history_max_tokens', 2000)
        object.__setattr__(settings, 'context_profile', 'legacy')
        messages = build_history(self.history(5), settings)
        self.assertEqual(len(messages), 4)
        self.assertTrue(messages[-1]['content'].startswith('# 报告'))


class Compaction(unittest.TestCase):
    def test_candidate_drops_the_long_intro(self):
        row = {'property_id': 'P001', 'community_name': '澄湾雅苑', 'price_yuan': 3580000,
               'area_sqm': 109.0, 'bedrooms': 3, 'floor': 8, 'total_floors': 18, 'intro': '很长' * 60}
        compacted = compact_candidate(row)
        self.assertNotIn('intro', compacted)
        self.assertEqual(compacted['property_id'], 'P001')

    def test_document_text_is_truncated(self):
        compacted = compact_document({'title': '澄湾雅苑', 'section': '交通', 'text': '字' * 900}, 400)
        self.assertEqual(len(compacted['text']), 400)


class ModelContext(unittest.TestCase):
    def test_memory_keeps_only_preferences_and_content(self):
        memory = {'preferences': {'bedrooms': 3}, 'preference_versions': {'bedrooms': {'version': 2}},
                  'semantic': [{'id': 'm1', 'content': '客户已退休', 'scope': {'user_id': 'c'},
                                'source': {'evidence': '原文'}, 'retention': {'pinned': False}}],
                  'episodic': [], 'evidence': [], 'retrieval_status': 'light'}
        compacted = compact_memory(memory)
        self.assertEqual(compacted['semantic'], [{'content': '客户已退休'}])
        self.assertNotIn('preference_versions', compacted)
        self.assertNotIn('evidence', compacted)
        self.assertEqual(compacted['preferences'], {'bedrooms': 3})

    def test_plan_drops_internal_route_flags(self):
        plan = {'route': 'parallel', 'query': '三室', 'amenities': ['小学'], 'exact_terms': [],
                'property_ids': [], 'community_names': [], 'model_route': 'parallel', 'host_route': 'listing_first',
                'soft_amenities_only': True, 'lookup_rejected': True, 'questions': ['预算？']}
        self.assertEqual(compact_plan(plan), {'route': 'parallel', 'query': '三室', 'amenities': ['小学'],
                                              'exact_terms': [], 'property_ids': [], 'community_names': []})

    def test_community_summary_does_not_repeat_documents(self):
        row = {'community_key': 'K1', 'community_name': '澄湾雅苑', 'region': '滨江区', 'score': 1.2,
               'covered_topics': ['小学'], 'unverified_topics': [], 'exact_terms': [], 'documents': [{'text': '很长' * 100}]}
        compacted = compact_community(row)
        self.assertNotIn('documents', compacted)
        self.assertNotIn('score', compacted)
        self.assertEqual(compacted['community_name'], '澄湾雅苑')


class ToolOutput(unittest.TestCase):
    def test_search_reply_keeps_comparison_fields_and_drops_the_intro(self):
        row = {'property_id': 'P001', 'community_name': '澄湾雅苑', 'price_yuan': 3580000,
               'orientation': '南北', 'decoration': '精装', 'total_floors': 18, 'intro': '很长' * 60}
        compacted = compact_tool_output('search_listings', {'status': 'ok', 'data': [row]})
        item = compacted['data'][0]
        self.assertEqual((item['orientation'], item['decoration'], item['total_floors']), ('南北', '精装', 18))
        self.assertNotIn('intro', item)

    def test_detail_reply_keeps_the_intro(self):
        row = {'property_id': 'P001', 'intro': '一段介绍'}
        compacted = compact_tool_output('get_property_details', {'status': 'ok', 'data': row})
        self.assertEqual(compacted['data']['intro'], '一段介绍')

    def test_errors_pass_through_untouched(self):
        payload = {'status': 'error', 'error_code': 'timeout'}
        self.assertEqual(compact_tool_output('search_listings', payload), payload)


class Summary(unittest.TestCase):
    def test_summary_keeps_rejections_ids_and_questions(self):
        history = [
            {'role': 'user', 'content': '不要一楼，预算400万'},
            {'role': 'assistant', 'content': '推荐 P001、CW-03。是否需要再看别的？'},
            {'role': 'user', 'content': '还是刚才那些条件'},
        ]
        payload = SessionMemoryContexts._summarize(history)
        self.assertIn('已否决', payload['text'])
        self.assertIn('P001', payload['text'])
        self.assertIn('待确认', payload['text'])
        self.assertLessEqual(len(payload['text']), 300)
        self.assertEqual(payload['messages'], 3)

    def test_empty_history_yields_empty_summary(self):
        payload = SessionMemoryContexts._summarize([])
        self.assertEqual(payload['text'], '')
        self.assertEqual(payload['messages'], 0)


if __name__ == '__main__':
    unittest.main()
