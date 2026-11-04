import unittest

from real_estate_desktop.eval.run import _aggregate_capability

class Metrics(unittest.TestCase):
    def row(self,id,passed=True,**checks):
        return {'id':id,'category':'recommend','polarity':'positive','passed':passed,'checks':checks,
                'reasons':[],'duration_seconds':1.5,'tool_calls':3,'reused_calls':1}
    def test_new_metric_names_and_efficiency_are_aggregated(self):
        metrics=_aggregate_capability([self.row('a'),self.row('b')],mode='scripted')
        self.assertEqual(metrics['samples'],2);self.assertEqual(metrics['task_success_rate'],1.0)
        self.assertEqual(metrics['efficiency']['mean_tool_calls'],3.0)
        self.assertEqual(metrics['efficiency']['reused_call_rate'],round(2/6,4))
        self.assertEqual(metrics['efficiency']['mean_tokens'],0.0)
        self.assertEqual(metrics['efficiency']['timeout_rate'],0.0)
    def test_judge_means_are_separate_from_programmatic_checks(self):
        first=self.row('a');first['judge']={'faithfulness':0.8,'answer_relevancy':0.9,'context_precision':0.7,
                                            'context_recall':0.6,'memory_faithfulness':1.0}
        second=self.row('b');second['judge']={'faithfulness':0.4,'answer_relevancy':0.5,'context_precision':0.3,
                                              'context_recall':0.2,'memory_faithfulness':0.0}
        metrics=_aggregate_capability([first,second],mode='online')
        self.assertEqual(metrics['judge_samples'],2)
        self.assertEqual(metrics['judge_faithfulness_rate'],0.6)
        self.assertEqual(metrics['judge_memory_faithfulness_rate'],0.5)
    def test_pass_k_requires_every_repeat_to_pass(self):
        rows=[self.row('a'),self.row('a',passed=False),self.row('b')]
        metrics=_aggregate_capability(rows,mode='scripted',repeats=2)
        self.assertEqual(metrics['samples'],2);self.assertEqual(metrics['runs'],3)
        self.assertEqual(metrics['pass_k_rate'],0.5)

if __name__=='__main__':unittest.main()
