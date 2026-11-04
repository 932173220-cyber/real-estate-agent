import asyncio,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from real_estate_desktop.runtime import Runtime
from real_estate_desktop.config import Settings
from real_estate_desktop.catalog import Proposal,looks_like_intake
from real_estate_desktop.test_support import ScriptedModel,fixture_proposal

LISTING='添加房源：春江花园，小区：春江花园，区域：滨江区，总价350万，105平米，三室，楼层8/18层，有电梯，朝向南，精装，地铁600米。'
MATERIAL='添加小区资料，小区：春江花园，区域：滨江区。小区介绍：东门有公交站，南侧有公共绿地，周末可能较拥挤。来源：经纪人现场笔记，资料日期：2026-10-30'

class Catalog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration=3
        self.temp=tempfile.TemporaryDirectory();self.settings=Settings(Path(self.temp.name));self.r=Runtime(self.settings,model=ScriptedModel());self.s=self.r.catalog;self.row=self.s.create()
    async def asyncTearDown(self):await self.r.close();self.temp.cleanup()
    async def draft(self,text=LISTING):return await self.s.chat(self.row['entry_id'],text,'online')
    async def approve(self,row):return await self.s.confirm(row['entry_id'],row['revision'],True)
    async def test_natural_language_listing_is_preview_only(self):
        row=await self.draft();self.assertEqual(row['status'],'pending');self.assertEqual(len(self.r.business.properties()),200)
        p=row['payload']['property'];self.assertEqual(p['price_yuan'],3500000);self.assertEqual(p['area_sqm'],105);self.assertIsNone(p['building_age']);self.assertEqual(p['metro_distance_m'],600)
    async def test_missing_fields_are_asked_and_followup_completes(self):
        row=await self.draft('添加房源：春江花园，小区：春江花园，区域：滨江区，总价350万，105平米，三室')
        self.assertEqual(row['status'],'collecting');self.assertIn('总楼层',row['preview']);self.assertEqual(len(self.r.business.properties()),200)
        row=await self.draft('楼层8/18层，有电梯');self.assertEqual(row['status'],'pending');self.assertEqual(row['payload']['property']['price_yuan'],3500000)
    async def test_edits_invalidate_old_preview(self):
        row=await self.draft();revision=row['revision'];new=await self.draft('总价改成330万')
        # The test double follows explicit labelled amounts; latest exact value wins.
        with self.assertRaises(ValueError):await self.s.confirm(row['entry_id'],revision,True)
        self.assertEqual(len(self.r.business.properties()),200);self.assertGreater(new['revision'],revision);self.assertEqual(new['payload']['property']['price_yuan'],3300000)
    async def test_cancel_does_not_write_listing_or_material(self):
        row=await self.draft(LISTING+'\n'+MATERIAL);out=await self.s.confirm(row['entry_id'],row['revision'],False)
        self.assertEqual(out['status'],'cancelled');self.assertEqual(len(self.r.business.properties()),200);self.assertEqual(len(self.r.knowledge.documents()),120)
    async def test_listing_confirmation_is_idempotent(self):
        row=await self.draft();out=await self.approve(row);again=await self.approve(row)
        self.assertEqual(out['property_id'],again['property_id']);self.assertTrue(again['replayed']);self.assertEqual(len(self.r.business.properties()),201)
    async def test_listing_write_does_not_change_customer_preferences(self):
        before={c['customer_id']:self.r.profile(c['customer_id']) for c in self.r.customers()};row=await self.draft();await self.approve(row)
        self.assertEqual(before,{c['customer_id']:self.r.profile(c['customer_id']) for c in self.r.customers()})
    async def test_material_before_property_links_to_later_property(self):
        material=await self.draft(MATERIAL);self.assertEqual(material['status'],'pending');await self.approve(material)
        key=material['payload']['material']['community_key'];self.row=self.s.create();listing=await self.draft();await self.approve(listing)
        self.assertEqual(listing['payload']['property']['community_key'],key)
        docs=self.r.knowledge.search(key,'滨江区','公交站',False);self.assertTrue(docs);self.assertIn('东门有公交站',docs[0]['text']);self.assertIn('春江花园',docs[0]['title'])
    async def test_both_listing_and_material_commit_and_index(self):
        row=await self.draft(LISTING+'\n'+MATERIAL);self.assertEqual(row['status'],'pending');self.assertIn('property',row['payload']);self.assertIn('material',row['payload'])
        out=await self.approve(row);self.assertFalse(out['index_pending']);await self.approve(row)
        self.assertEqual(len(self.r.knowledge.documents()),121);self.assertEqual(len(self.r.business.properties()),201)
    async def test_index_failure_leaves_retryable_job(self):
        row=await self.draft(LISTING+'\n'+MATERIAL)
        with patch.object(self.r.knowledge,'import_text',side_effect=OSError('disk busy')):out=await self.approve(row)
        self.assertTrue(out['index_pending']);self.assertEqual(len(self.r.business.properties()),201);self.assertEqual(len(self.s.store.jobs(self.r.broker)),1)
        await asyncio.gather(self.s.retry_index(),self.s.retry_index());self.assertEqual(len(self.r.knowledge.documents()),121);self.assertFalse(self.s.store.jobs(self.r.broker))
    async def test_restart_retains_preview(self):
        row=await self.draft();await self.r.close();self.r=Runtime(self.settings,model=ScriptedModel());self.s=self.r.catalog
        self.assertEqual(self.s.get(row['entry_id'])['status'],'pending');await self.approve(row);self.assertEqual(len(self.r.business.properties()),201)
    async def test_broker_scope_and_revision_guard(self):
        row=await self.draft()
        with self.assertRaises(PermissionError):self.s.store.confirm('different-broker',row['entry_id'],row['revision'],True)
        with self.assertRaises(ValueError):await self.s.confirm(row['entry_id'],row['revision'],'yes')
        self.assertFalse(self.s.store.pending('different-broker'))
    async def test_invalid_floor_cannot_be_committed(self):
        row=await self.draft(LISTING.replace('8/18','28/18'));self.assertEqual(row['status'],'collecting');self.assertIn('楼层矛盾',row['preview'])
        with self.assertRaises(ValueError):await self.approve(row)
    async def test_scripted_llm_path_extracts_with_verified_quotes(self):
        proposal=fixture_proposal([{'role':'user','content':LISTING}])
        class Model:
            async def generate(self,messages,tools,**kw):
                self.messages=messages;self.tools=tools
                return {'role':'assistant','content':'','tool_calls':[{'id':'proposal','type':'function','function':{'name':'propose_catalog_entry','arguments':proposal.model_dump_json()}}]}
            async def aclose(self):pass
        self.r.model=Model();row=await self.s.chat(self.row['entry_id'],LISTING,'online')
        self.assertEqual(row['status'],'pending');self.assertIn('在线模型整理',row['preview']);self.assertEqual(self.r.model.tools[0]['function']['name'],'propose_catalog_entry');self.assertEqual(len(self.r.business.properties()),200)
    async def test_fabricated_numeric_field_is_not_accepted(self):
        proposal=fixture_proposal([{'role':'user','content':LISTING}]);proposal.property.price_yuan=350
        row=self.s.store.append(self.r.broker,self.row['entry_id'],LISTING);payload,reply,ready=self.s.prepare(row,proposal,'online')
        self.assertFalse(ready);self.assertEqual(payload,{});self.assertIn('总价',reply)
    async def test_model_cannot_fabricate_elevator_or_material_body(self):
        text=LISTING.replace('，有电梯','');proposal=fixture_proposal([{'role':'user','content':text}]);proposal.property.has_elevator=True;proposal.property.evidence['has_elevator']='有电梯'
        row=self.s.store.append(self.r.broker,self.row['entry_id'],text);_,reply,ready=self.s.prepare(row,proposal,'online');self.assertFalse(ready);self.assertIn('有无电梯',reply)
        proposal=Proposal.model_validate({'target':'material','material':{'community_name':'春江花园','region':'滨江区','text':'保证所有业主孩子均可入学'}})
        row=self.s.store.append(self.r.broker,self.row['entry_id'],MATERIAL);_,reply,ready=self.s.prepare(row,proposal,'online');self.assertFalse(ready);self.assertIn('资料原文',reply)
    async def test_main_customer_chat_redirects_without_memory_pollution(self):
        cid=self.r.customers()[0]['customer_id'];sid=self.r.new_session(cid);out=await self.r.chat(LISTING,cid,sid)
        self.assertTrue(out.get('catalog_redirect'));self.assertEqual(self.r.history(cid,sid),[])
    async def test_material_reuses_existing_property_community_key(self):
        p={**self.r.business.properties()[0],'property_id':'R-existing','community_name':'春江花园','region':'滨江区','community_key':'existing-key','is_demo':False};self.r.business.put_properties([p])
        row=await self.draft(MATERIAL);self.assertEqual(row['payload']['material']['community_key'],'existing-key')
    async def test_expired_entry_cannot_be_committed(self):
        row=await self.draft()
        with self.r.business.connection() as con:con.execute('UPDATE catalog_drafts SET expires=0 WHERE entry_id=?',(row['entry_id'],))
        with self.assertRaises(ValueError):await self.approve(row)
    async def test_no_model_does_not_generate_local_preview(self):
        self.r.model=None
        with self.assertRaisesRegex(ValueError,'尚未配置在线模型'):await self.s.chat(self.row['entry_id'],LISTING)
        self.assertEqual(self.s.get(self.row['entry_id'])['status'],'collecting');self.assertEqual(len(self.r.business.properties()),200)
    async def test_local_mode_rejected_and_api_failure_keeps_only_raw_draft(self):
        with self.assertRaisesRegex(ValueError,'仅支持在线模型'):await self.s.chat(self.row['entry_id'],LISTING,'local')
        self.assertEqual(self.s.get(self.row['entry_id'])['messages'],[])
        with patch.object(self.r.model,'generate',side_effect=ConnectionError('network unavailable')):
            with self.assertRaises(ConnectionError):await self.s.chat(self.row['entry_id'],LISTING)
        row=self.s.get(self.row['entry_id']);self.assertEqual(row['status'],'collecting');self.assertEqual(row['payload'],{});self.assertEqual(row['messages'][0]['content'],LISTING)

if __name__=='__main__':unittest.main()


