import tempfile,unittest
from pathlib import Path
from real_estate_desktop.config import Settings
from real_estate_desktop.runtime import Runtime
from real_estate_desktop.test_support import ScriptedModel

class TypedModel(ScriptedModel):
    def memory_facts(self,text):
        if '退休' in text:
            return [{'memory_type':'semantic','attribute':'employment','value':'退休','content':'客户已经退休','evidence':'客户已经退休'}]
        if '返聘' in text:
            return [{'memory_type':'semantic','attribute':'employment','value':'返聘上班','content':'客户目前返聘上班','evidence':'客户目前返聘上班'}]
        return []

class Fixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.settings=Settings(Path(self.temp.name))
        self.r=Runtime(self.settings,model=ScriptedModel())
        self.cid=self.r.create_customer('撤销测试客户')['customer_id'];self.sid=self.r.new_session(self.cid)
    async def asyncTearDown(self):await self.r.close();self.temp.cleanup()
    async def save(self,text):
        out=await self.r.chat(text,self.cid,self.sid);self.assertTrue(out['draft_id'])
        result=await self.r.confirm(self.cid,self.sid,out['draft_id'],True);self.assertEqual(result['status'],'saved')
        return out['draft_id']

class Undo(Fixture):
    async def test_undo_removes_typed_memory_from_the_latest_batch(self):
        self.r.model=TypedModel()
        out=await self.r.chat('只记住客户：客户已经退休',self.cid,self.sid)
        self.assertTrue(out['draft_id']);await self.r.confirm(self.cid,self.sid,out['draft_id'],True)
        self.assertEqual(self.r.memory_records(self.cid)['semantic'][0]['value'],'退休')
        result=await self.r.undo_last_write(self.cid)
        self.assertGreaterEqual(result['removed'],1)
        self.assertEqual(self.r.memory_records(self.cid)['semantic'],[])
        self.assertEqual(self.r.records(self.cid),[])
    async def test_undo_restores_the_previous_typed_version(self):
        self.r.model=TypedModel()
        first=await self.r.chat('只记住客户：客户已经退休',self.cid,self.sid);await self.r.confirm(self.cid,self.sid,first['draft_id'],True)
        second=await self.r.chat('只记住客户：客户目前返聘上班',self.cid,self.sid);await self.r.confirm(self.cid,self.sid,second['draft_id'],True)
        self.assertEqual(self.r.memory_records(self.cid)['semantic'][0]['value'],'返聘上班')
        result=await self.r.undo_last_write(self.cid)
        self.assertGreaterEqual(result['restored'],1)
        current=self.r.memory_records(self.cid)['semantic'];self.assertEqual([row['value'] for row in current],['退休'])
    async def test_undo_restores_the_previous_preference_version(self):
        first=await self.save('预算400万，三室，不要一楼')
        second=await self.save('预算350万，三室')
        self.assertEqual(self.r.profile(self.cid)['max_price_yuan'],3500000);self.assertEqual(len(self.r.records(self.cid)),2)
        result=await self.r.undo_last_write(self.cid)
        self.assertGreaterEqual(result['keys'],1);self.assertEqual(len(self.r.records(self.cid)),1);self.assertEqual(len(self.r.followups(self.cid)),1)
        self.assertEqual(self.r.profile(self.cid)['max_price_yuan'],4000000)
        active=[row for row in self.r.preference_versions(self.cid) if row['key']=='max_price_yuan' and row['active']]
        self.assertEqual(len(active),1);self.assertEqual(active[0]['version'],1)
        self.assertEqual(self.r.business.memory_jobs(self.r.broker,self.cid),[])
        undone=await self.r.confirm(self.cid,self.sid,second,True);self.assertEqual(undone['status'],'undone')
        self.assertEqual(len(self.r.records(self.cid)),1)
    async def test_undo_of_a_new_preference_leaves_it_empty(self):
        await self.save('预算400万，三室')
        await self.r.undo_last_write(self.cid)
        self.assertEqual(self.r.records(self.cid),[]);self.assertEqual(self.r.profile(self.cid),{})
        self.assertEqual(self.r.memory.recall_light(self.r.broker,self.cid)['preferences'],{})
    async def test_undo_without_saved_record_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'没有可撤销'):await self.r.undo_last_write(self.cid)

if __name__=='__main__':unittest.main()
