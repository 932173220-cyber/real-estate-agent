import unittest

from real_estate_desktop.eval.judge import GroundingJudge

class Fake:
    def __init__(self,payload):self.payload=payload;self.data=None
    async def memory_json(self,prompt,data,schema):self.data=data;return self.payload

class Judge(unittest.IsolatedAsyncioTestCase):
    async def test_scores_are_clamped_and_lists_are_bounded(self):
        fake=Fake({'faithfulness':1.4,'answer_relevancy':-1,'context_precision':0.5,'context_recall':0.5,
                   'memory_faithfulness':0.9,'unsupported_claims':['x']*20,'hallucinated_memories':[]})
        judge=GroundingJudge(fake)
        observed={'report':'报告','documents':[{'title':'T','section':'S','text':'资料'}],
                  'memory_records':{'semantic':[{'content':'c','value':'v','source':{'evidence':'c'}}],'episodic':[]},
                  'history':[{'role':'user','content':'c'}]}
        result=await judge.score({'text':'问题'},observed)
        self.assertEqual(result['faithfulness'],1.0);self.assertEqual(result['answer_relevancy'],0.0)
        self.assertEqual(len(result['unsupported_claims']),10)
        self.assertEqual(fake.data['question'],'问题')

if __name__=='__main__':unittest.main()
