import tempfile,unittest
from pathlib import Path
from real_estate_desktop.config import Settings
from real_estate_desktop.runtime import Runtime
from real_estate_desktop.test_support import ScriptedModel

class FollowUpModel(ScriptedModel):
    def __init__(self,payload):
        self.payload=payload;self.seen=None
    async def extract_json(self,prompt,data,schema):
        self.seen=data
        return self.payload

class Fixture(unittest.IsolatedAsyncioTestCase):
    payload=None
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.settings=Settings(Path(self.temp.name))
        self.model=FollowUpModel(self.payload or {'demand_timeline':[],'viewed':[],'open_items':[],'summary':'本次会话已完成沟通。'})
        self.r=Runtime(self.settings,model=self.model)
        self.cid=self.r.create_customer('跟进客户')['customer_id'];self.sid=self.r.new_session(self.cid)
        self.a,self.b,self.c=[p for p in self.r.business.properties()[:3]]
        self.broker=self.r.broker
    async def asyncTearDown(self):await self.r.close();self.temp.cleanup()
    def say(self,role,text,message_id):self.r.business.append(self.broker,self.cid,self.sid,role,text,message_id)
    async def followup(self):return await self.r.chat('生成跟进记录',self.cid,self.sid)

class ModelExtraction(Fixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.model.payload={'demand_timeline':['预算300万，三室'],
            'viewed':[{'property_id':self.a['property_id'],'feedback':'客户觉得通勤方便'},
                {'property_id':self.b['property_id'],'feedback':'客户非常满意'},
                {'property_id':'P999','feedback':'客户已成交'}],
            'open_items':['核实小区到地铁的步行时间'],
            'summary':'本次沟通明确了预算和户型，通勤仍需核实。'}
    async def test_draft_is_model_extracted_and_host_grounded(self):
        self.say('user','客户觉得通勤方便，预算300万，三室','m1')
        self.say('assistant',f"已推荐 {self.a['property_id']}（{self.a['community_name']}）、{self.b['property_id']}（{self.b['community_name']}）。",'m2')
        out=await self.followup()
        report=out['report']
        self.assertTrue(out['draft_id'])
        self.assertIn('# 跟进记录草稿',report);self.assertIn('## 需求脉络',report);self.assertIn('## 已沟通房源',report)
        self.assertIn(f"{self.a['property_id']} · {self.a['community_name']}",report)
        self.assertIn('客户原话：客户觉得通勤方便',report)
        self.assertIn('## 待办事项',report);self.assertIn('核实小区到地铁的步行时间',report)
        self.assertIn('本次沟通明确了预算和户型',report)
        # The ungrounded praise is cleared and the unknown property is dropped.
        self.assertNotIn('客户非常满意',report);self.assertNotIn('- P999',report)
        self.assertIn('已省略无法在会话原文中核对的反馈',report);self.assertIn('已省略会话候选之外的房源',report)
        # Candidate fields are host-computed, not model-supplied.
        seen=self.model.seen
        self.assertEqual(set(seen),{'conversation','preference_versions','session_records','session_candidates'})
        self.assertEqual(seen['session_candidates'],sorted([self.a['property_id'],self.b['property_id']]))
        self.assertEqual(self.r.records(self.cid),[])
        result=await self.r.confirm(self.cid,self.sid,out['draft_id'],True)
        self.assertEqual(result['status'],'saved');self.assertEqual(len(self.r.followups(self.cid)),1)
        saved=self.r.followups(self.cid)[0]['content']
        self.assertIn('本次沟通明确了预算和户型',saved);self.assertNotIn('客户非常满意',saved)

class SessionRecords(Fixture):
    async def test_confirmed_recommendation_is_part_of_the_input(self):
        payload={'report':'# 房源推荐报告','properties':[self.a],'documents':[],'source_text':'预算300万，三室',
            'preference_updates':{},'preference_source':'预算300万，三室','kind':'recommend','mode':'online'}
        draft=self.r.business.prepare(self.broker,self.cid,self.sid,'seed-request',payload)
        self.r.business.commit(self.broker,self.cid,self.sid,draft['draft_id'],True)
        out=await self.followup()
        self.assertTrue(out['draft_id'])
        records=self.model.seen['session_records']
        self.assertEqual([item['property_id'] for item in records[0]['properties']],[self.a['property_id']])
        self.assertIn(self.a['property_id'],self.model.seen['session_candidates'])

class Gate(Fixture):
    async def test_no_draft_or_followup_before_confirmation(self):
        self.say('user','客户觉得通勤方便','m1')
        out=await self.followup();self.assertTrue(out['draft_id'])
        self.assertEqual(self.r.records(self.cid),[]);self.assertEqual(self.r.followups(self.cid),[])
        await self.r.confirm(self.cid,self.sid,out['draft_id'],False)
        self.assertEqual(self.r.records(self.cid),[]);self.assertEqual(self.r.followups(self.cid),[])
    async def test_property_outside_this_session_is_dropped(self):
        self.payload={'demand_timeline':[],'open_items':[],'summary':'本次会话已沟通。',
            'viewed':[{'property_id':self.c['property_id'],'feedback':'客户觉得通勤方便'}]}
        self.model.payload=self.payload
        self.say('user','客户觉得通勤方便，预算300万','m1')
        out=await self.followup()
        self.assertNotIn(f"- {self.c['property_id']}",out['report'])
        self.assertIn('已省略会话候选之外的房源',out['report'])
    async def test_missing_model_fails_before_any_write(self):
        await self.r.close();self.r=Runtime(self.settings,model=None)
        self.say('user','预算300万，三室','m1')
        with self.assertRaisesRegex(ValueError,'尚未配置在线模型'):await self.followup()
        self.assertEqual(self.r.followups(self.cid),[]);self.assertEqual(self.r.records(self.cid),[])
    async def test_invalid_payload_is_rejected_before_any_draft(self):
        self.model.payload={'summary':'','demand_timeline':[]}
        with self.assertRaisesRegex(ValueError,'不符合格式'):await self.followup()
        self.assertFalse(self.r.pending(self.cid))

if __name__=='__main__':unittest.main()
