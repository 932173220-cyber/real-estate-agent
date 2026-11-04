import json,tempfile,unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock,patch
from real_estate_desktop.config import Settings
from real_estate_desktop.runtime import Runtime
from real_estate_desktop.test_support import ScriptedModel
from real_estate_desktop.tools import Context

class ReAct(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.r=Runtime(Settings(Path(self.temp.name)),model=ScriptedModel())
        self.cid=self.r.create_customer('ReAct客户')['customer_id'];self.sid=self.r.new_session(self.cid)
        self.ctx=Context(self.r.broker,self.cid,self.sid,'react-request')
    async def asyncTearDown(self):await self.r.close();self.temp.cleanup()
    def state(self,documents=None,reflection_count=0):
        prop=self.r.business.properties()[0];self.r.tools.candidates[self.ctx.request_id]=[prop]
        return {'identity':asdict(self.ctx),'text':'预算400万，三室，不要一楼','request':{'intent':'recommend'},
                'properties':[prop],'documents':documents or [],'warnings':[],'reflection_count':reflection_count}
    async def test_think_records_a_thought_and_a_trace_step(self):
        result=await self.r.tools.execute('think',{'thought':'先核对候选，再补查资料','next':'retrieve_area_info'},self.ctx)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(self.r.tools.thoughts[self.ctx.request_id][0]['thought'],'先核对候选，再补查资料')
        self.assertEqual(self.r.tools.trace(self.ctx)[-1]['tool'],'think')
        self.assertEqual(self.r.tools.trace(self.ctx)[-1]['thought'],'先核对候选，再补查资料')
    async def test_reflect_retries_missing_evidence_once_then_reanalyzes(self):
        document={'chunk_id':'d1','title':'小区资料','section':'交通','community_key':'K','as_of':'2026-10-30','page':1,'text':'步行十分钟到地铁。'}
        with patch.object(self.r.tools,'execute',new=AsyncMock(return_value={'status':'ok','data':[document]})) as execute:
            first=await self.r.workflow.reflect(self.state())
            second=await self.r.workflow.reflect(self.state(documents=[document],reflection_count=1))
        self.assertTrue(first['need_more']);self.assertEqual(first['reflection_count'],1);self.assertEqual(first['documents'],[document])
        self.assertFalse(second['need_more']);self.assertEqual(execute.await_count,1)
    async def test_analyze_enters_reflect(self):
        await self.r.chat('预算400万，三室，不要一楼，不保存',self.cid,self.sid)
        path=self.r.settings.data_dir/'logs/tools.jsonl';events=[json.loads(line) for line in path.read_text('utf-8').splitlines()]
        self.assertTrue(any(event['tool']=='workflow' and event['event']=='reflect_enter' for event in events))
    async def test_reflect_stops_when_retry_has_no_evidence(self):
        with patch.object(self.r.tools,'execute',new=AsyncMock(return_value={'status':'empty','data':[]})):
            result=await self.r.workflow.reflect(self.state())
        self.assertFalse(result['need_more']);self.assertEqual(result['reflection_count'],1)
        self.assertTrue(any('待核验' in warning for warning in result['warnings']))

if __name__=='__main__':unittest.main()
