import tempfile,unittest
from pathlib import Path
from real_estate_desktop.config import Settings
from real_estate_desktop.runtime import Runtime
from real_estate_desktop.test_support import ScriptedModel
from real_estate_desktop.tools import Context

class TypedModel(ScriptedModel):
    def memory_facts(self,text):
        if '退休' in text:
            return [{'memory_type':'semantic','attribute':'employment','value':'退休','content':'客户已经退休','evidence':'客户已经退休'}]
        return []

class Fixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.settings=Settings(Path(self.temp.name))
        self.r=Runtime(self.settings,model=TypedModel())
        self.cid=self.r.create_customer('记忆管理客户')['customer_id'];self.sid=self.r.new_session(self.cid)
    async def asyncTearDown(self):await self.r.close();self.temp.cleanup()

class Preferences(Fixture):
    async def test_edit_creates_a_new_active_version(self):
        self.r.memory.apply(self.r.broker,self.cid,{'max_price_yuan':4000000},'原预算400万','seed')
        self.r.update_preference(self.cid,'max_price_yuan',3500000)
        versions=self.r.preference_versions(self.cid)
        self.assertEqual(len(versions),2)
        active=[row for row in versions if row['active']]
        self.assertEqual(len(active),1);self.assertEqual(active[0]['version'],2)
        self.assertEqual(__import__('json').loads(active[0]['value']),3500000)
        self.assertEqual(self.r.profile(self.cid)['max_price_yuan'],3500000)
    async def test_invalid_value_and_unknown_key_are_rejected(self):
        with self.assertRaises(ValueError):self.r.update_preference(self.cid,'bedrooms',99)
        with self.assertRaises(ValueError):self.r.update_preference(self.cid,'not_a_field','x')
    async def test_delete_removes_it_from_profile_and_recall(self):
        self.r.memory.apply(self.r.broker,self.cid,{'max_price_yuan':4000000},'原预算400万','seed')
        record=self.r.preference_versions(self.cid)[0]
        self.assertTrue(self.r.delete_memory_record(self.cid,record['memory_id']))
        self.assertEqual(self.r.profile(self.cid),{})
        recall=self.r.memory.recall_light(self.r.broker,self.cid)
        self.assertEqual(recall['preferences'],{})
        self.assertFalse(any(row['active'] for row in self.r.preference_versions(self.cid)))
    async def test_manual_edit_invalidates_the_cached_session_snapshot(self):
        ctx=Context(self.r.broker,self.cid,self.sid,'cache-request')
        first=await self.r.tools.execute('query_customer_memory',{'query':'偏好','phase':'light'},ctx)
        self.assertEqual(first['data']['preferences'],{})
        self.r.memory.apply(self.r.broker,self.cid,{'max_price_yuan':4000000},'原预算400万','seed')
        self.r.update_preference(self.cid,'max_price_yuan',3500000)
        second=await self.r.tools.execute('query_customer_memory',{'query':'偏好','phase':'light'},ctx)
        self.assertEqual(second['data']['preferences']['max_price_yuan'],3500000)
    async def test_change_log_records_add_update_and_noop_with_values(self):
        self.r.memory.apply(self.r.broker,self.cid,{'max_price_yuan':4000000},'客户说400万','batch1')
        self.r.memory.apply(self.r.broker,self.cid,{'max_price_yuan':3500000},'客户改为350万','batch2')
        self.r.memory.apply(self.r.broker,self.cid,{'max_price_yuan':3500000},'客户重复确认','batch3')
        rows=self.r.memory_changes(self.cid)
        self.assertEqual([row['action'] for row in rows[:3]],['NOOP','UPDATE','ADD'])
        update=rows[1];self.assertEqual(__import__('json').loads(update['old_value']),4000000);self.assertEqual(__import__('json').loads(update['new_value']),3500000)
        self.assertEqual(update['source'],'客户改为350万');self.assertEqual(rows[2]['old_value'],None)
    async def test_manual_edit_and_delete_are_logged_with_source(self):
        self.r.memory.apply(self.r.broker,self.cid,{'max_price_yuan':4000000},'客户说400万','batch1')
        self.r.update_preference(self.cid,'max_price_yuan',3500000)
        rows=self.r.memory_changes(self.cid)
        self.assertEqual(rows[0]['action'],'UPDATE');self.assertEqual(rows[0]['source'],'经纪人手动修改')
        record=self.r.preference_versions(self.cid)[0]
        self.r.delete_memory_record(self.cid,record['memory_id'])
        self.assertEqual(self.r.memory_changes(self.cid)[0]['action'],'DELETE')
    async def test_clear_is_restart_safe(self):
        self.r.memory.apply(self.r.broker,self.cid,{'max_price_yuan':4000000,'bedrooms':3},'原偏好','seed')
        result=self.r.clear_memory(self.cid)
        self.assertEqual(result['preferences'],2);self.assertEqual(self.r.profile(self.cid),{});self.assertEqual(self.r.preference_versions(self.cid),[])
        self.assertEqual(self.r.memory.recall_light(self.r.broker,self.cid)['preferences'],{})

class Typed(Fixture):
    async def test_edit_typed_record_creates_a_new_version(self):
        out=await self.r.chat('只记住客户：客户已经退休',self.cid,self.sid);await self.r.confirm(self.cid,self.sid,out['draft_id'],True)
        old=self.r.memory_records(self.cid)['semantic'][0]
        updated=self.r.update_memory_record(self.cid,old['id'],content='客户已经退休，时间比较充裕',value='退休')
        self.assertEqual(updated['content'],'客户已经退休，时间比较充裕')
        current=self.r.memory_records(self.cid)['semantic'];self.assertEqual([row['id'] for row in current],[updated['id']])
        history=self.r.memory_records(self.cid,history=True)['semantic']
        self.assertEqual(next(row['status'] for row in history if row['id']==old['id']),'superseded')
        self.assertTrue(any(row['memory_type']=='semantic' and row['action']=='UPDATE' for row in self.r.memory_changes(self.cid)))
    async def test_delete_typed_record_removes_it_from_context(self):
        out=await self.r.chat('只记住客户：客户已经退休',self.cid,self.sid)
        self.assertTrue(out['draft_id']);await self.r.confirm(self.cid,self.sid,out['draft_id'],True)
        changes=self.r.memory_changes(self.cid)
        self.assertTrue(any(row['memory_type']=='semantic' and row['key']=='employment' and row['action']=='ADD' for row in changes))
        records=self.r.memory_records(self.cid)['semantic'];self.assertEqual(len(records),1)
        self.assertTrue(self.r.delete_memory_record(self.cid,records[0]['id']))
        self.assertEqual(self.r.memory_records(self.cid)['semantic'],[])
        self.assertEqual(self.r.memory_records(self.cid,history=True)['semantic'],[])
    async def test_clear_removes_typed_records(self):
        out=await self.r.chat('只记住客户：客户已经退休',self.cid,self.sid);await self.r.confirm(self.cid,self.sid,out['draft_id'],True)
        result=self.r.clear_memory(self.cid)
        self.assertGreaterEqual(result['typed'],1)
        self.assertEqual(self.r.memory_records(self.cid),{'semantic':[],'episodic':[]})

if __name__=='__main__':unittest.main()
