import json,tempfile,unittest
from pathlib import Path
from real_estate_desktop.config import Settings
from real_estate_desktop.runtime import Runtime
from real_estate_desktop.test_support import ScriptedModel

class RevisionModel(ScriptedModel):
    def __init__(self):
        self.selection=None;self.seen=None
    async def extract_json(self,prompt,data,schema):
        if schema.get('title')=='DraftRevision':
            self.seen=data
            current=[item['property_id'] for item in data.get('current_draft',{}).get('properties',[])]
            return {'property_ids':current if self.selection is None else self.selection,'note':'按经纪人意见调整。'}
        return await super().extract_json(prompt,data,schema)

class Fixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.settings=Settings(Path(self.temp.name))
        self.model=RevisionModel();self.r=Runtime(self.settings,model=self.model)
        self.cid=self.r.create_customer('改稿客户')['customer_id'];self.sid=self.r.new_session(self.cid)
    async def asyncTearDown(self):await self.r.close();self.temp.cleanup()
    async def initial(self):
        return await self.r.chat('预算400万以内，三室，不要一楼，最好离地铁近',self.cid,self.sid)
    def payload(self,draft_id):
        return json.loads(self.r.business.draft(self.r.broker,self.cid,self.sid,draft_id)['payload'])

class Revision(Fixture):
    async def test_removing_a_property_creates_a_new_superseding_draft(self):
        first=await self.initial();old=first['draft_id'];self.assertTrue(old)
        ids=[item['property_id'] for item in self.payload(old)['properties']]
        self.assertEqual(len(ids),3)
        self.model.selection=[ids[0],ids[2]]
        second=await self.r.chat(f'去掉 {ids[1]}，改一下',self.cid,self.sid)
        new=second['draft_id'];self.assertTrue(new);self.assertNotEqual(new,old)
        self.assertEqual(self.r.business.draft(self.r.broker,self.cid,self.sid,old)['status'],'superseded')
        self.assertEqual([item['property_id'] for item in self.payload(new)['properties']],[ids[0],ids[2]])
        self.assertNotIn(f"### {ids[1]}",second['report'])
        self.assertIn(f"### {ids[0]}",second['report'])
        stale=await self.r.confirm(self.cid,self.sid,old,True)
        self.assertEqual(stale['status'],'superseded');self.assertEqual(self.r.records(self.cid),[])
        saved=await self.r.confirm(self.cid,self.sid,new,True)
        self.assertEqual(saved['status'],'saved')
        records=self.r.records(self.cid);self.assertEqual(len(records),1)
        self.assertEqual([item['property_id'] for item in json.loads(records[0]['payload'])['properties']],[ids[0],ids[2]])
        self.assertEqual(len(self.r.followups(self.cid)),1)
    async def test_new_hard_condition_researches_and_supersedes_old(self):
        first=await self.initial();old=first['draft_id']
        second=await self.r.chat('预算改成300万',self.cid,self.sid)
        new=second['draft_id'];self.assertTrue(new);self.assertNotEqual(new,old)
        self.assertEqual(self.r.business.draft(self.r.broker,self.cid,self.sid,old)['status'],'superseded')
        self.assertLessEqual(max(item['price_yuan'] for item in self.payload(new)['properties']),3000000)
        self.assertEqual((await self.r.confirm(self.cid,self.sid,old,True))['status'],'superseded')
        self.assertEqual(self.r.records(self.cid),[])
    async def test_unmentioned_candidate_cannot_be_added_by_the_model(self):
        first=await self.initial();old=first['draft_id']
        ids=[item['property_id'] for item in self.payload(old)['properties']]
        extra=next(p for p in self.r.business.properties() if p['property_id'] not in ids)
        self.r.business.append(self.r.broker,self.cid,self.sid,'assistant',f"另可查看 {extra['property_id']}。",'note-extra')
        self.model.selection=[ids[0],extra['property_id']]
        second=await self.r.chat('改一下，保留合适的',self.cid,self.sid)
        self.assertTrue(second['draft_id'])
        self.assertEqual([item['property_id'] for item in self.payload(second['draft_id'])['properties']],[ids[0]])
        self.assertIn(extra['property_id'],[item['property_id'] for item in self.model.seen['session_candidates']])

if __name__=='__main__':unittest.main()
