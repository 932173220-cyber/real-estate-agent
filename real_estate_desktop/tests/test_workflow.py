from real_estate_desktop.test_support import ScriptedModel
import asyncio,json,sqlite3,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from real_estate_desktop.config import Settings
from real_estate_desktop.runtime import Runtime
from real_estate_desktop.tools import Context
from real_estate_desktop.domain import parse_request,merge_preferences
from real_estate_desktop.app import read_properties
from real_estate_desktop.skill_registry import CATALOGUE,load_skill

class Fixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration=3
        self.temp=tempfile.TemporaryDirectory();self.settings=Settings(Path(self.temp.name));self.r=Runtime(self.settings,model=ScriptedModel())
        self.c=self.r.create_customer('测试客户甲');self.cid=self.c['customer_id'];self.sid=self.r.new_session(self.cid)
        self.other=self.r.create_customer('测试客户乙')['customer_id'];self.other_sid=self.r.new_session(self.other)
        self.ctx=Context(self.r.broker,self.cid,self.sid,'test-request')
    async def asyncTearDown(self):await self.r.close();self.temp.cleanup()
    async def draft(self,text='预算400万以内，三室，不要一楼，最好离地铁近'):
        return await self.r.chat(text,self.cid,self.sid)
    async def test_complete_confirmation_and_idempotence(self):
        out=await self.draft();self.assertTrue(out['draft_id']);self.assertEqual(self.r.records(self.cid),[]);self.assertEqual(self.r.profile(self.cid),{})
        result=await self.r.confirm(self.cid,self.sid,out['draft_id'],True);self.assertEqual(result['memory_status'],'ok')
        again=await self.r.confirm(self.cid,self.sid,out['draft_id'],True);self.assertTrue(again['replayed']);self.assertEqual(again['record_id'],result['record_id'])
        self.assertEqual(len(self.r.records(self.cid)),1);self.assertEqual(len(self.r.followups(self.cid)),1);self.assertEqual(self.r.profile(self.cid)['max_price_yuan'],4000000)
    async def test_cancel_has_no_business_or_memory_writes(self):
        out=await self.draft();result=await self.r.confirm(self.cid,self.sid,out['draft_id'],False)
        self.assertEqual(result['status'],'cancelled');self.assertEqual(self.r.records(self.cid),[]);self.assertEqual(self.r.profile(self.cid),{})
    async def test_no_save_is_explicitly_honored(self):
        out=await self.draft('预算300万，两室，只查询，不保存');self.assertIsNone(out['draft_id']);self.assertFalse(self.r.pending(self.cid))
    async def test_restart_resumes_pending_checkpoint(self):
        out=await self.draft();await self.r.close();self.r=Runtime(self.settings,model=ScriptedModel())
        result=await self.r.confirm(self.cid,self.sid,out['draft_id'],True);self.assertEqual(result['status'],'saved');self.assertEqual(len(self.r.records(self.cid)),1)
    async def test_customer_confirmation_is_bound(self):
        out=await self.draft()
        with self.assertRaises(PermissionError):await self.r.confirm(self.other,self.other_sid,out['draft_id'],True)
        second=self.r.new_session(self.cid)
        with self.assertRaises(PermissionError):await self.r.confirm(self.cid,second,out['draft_id'],True)
        self.assertEqual(self.r.records(self.cid),[])
    async def test_broker_access_is_bound(self):
        with self.assertRaises(PermissionError):self.r.business.customer('other-broker',self.cid)
        with self.assertRaises(PermissionError):self.r.business.history('other-broker',self.cid,self.sid)
    async def test_same_name_customers_have_distinct_ids(self):
        c=self.r.create_customer('测试客户甲');self.assertNotEqual(c['customer_id'],self.cid)
    async def test_session_cannot_switch_customer(self):
        with self.assertRaises(PermissionError):await self.r.chat('查看偏好',self.other,self.sid)
    async def test_memory_is_scoped(self):
        self.r.memory.apply(self.r.broker,self.cid,{'bedrooms':3},'甲要三室','m1');self.r.memory.apply(self.r.broker,self.other,{'bedrooms':2},'乙要两室','m2')
        self.assertEqual(self.r.memory.recall(self.r.broker,self.cid,'户型')['preferences'],{'bedrooms':3});self.assertEqual(self.r.profile(self.other),{'bedrooms':2})
        self.assertEqual(self.r.memory.profile('other-broker',self.cid),{})
    async def test_new_conversation_reuses_confirmed_preferences(self):
        out=await self.draft();await self.r.confirm(self.cid,self.sid,out['draft_id'],True);sid=self.r.new_session(self.cid)
        result=await self.r.chat('帮我推荐房源，不保存',self.cid,sid);self.assertIn('预算上限：400万元',result['report']);self.assertIn('最低楼层：2',result['report'])
    async def test_current_conversation_keeps_unconfirmed_constraints(self):
        await self.draft('预算350万以内，三室，不要一楼，不保存');result=await self.draft('优先地铁近，不保存')
        self.assertIn('预算上限：350万元',result['report']);self.assertIn('卧室数：3',result['report']);self.assertEqual(self.r.profile(self.cid),{})
    async def test_clear_and_version_preferences(self):
        self.r.memory.apply(self.r.broker,self.cid,{'max_price_yuan':4000000},'预算400万','a')
        self.r.memory.apply(self.r.broker,self.cid,{'max_price_yuan':3500000},'改为350万','b')
        self.r.memory.apply(self.r.broker,self.cid,{'max_price_yuan':None},'预算不限','c')
        self.assertEqual(self.r.profile(self.cid),{});versions=self.r.preference_versions(self.cid);self.assertEqual(len(versions),3);self.assertEqual(sum(v['active'] for v in versions),1)
    async def test_memory_failures_are_retried_without_duplicate_record(self):
        out=await self.draft()
        with patch.object(self.r.memory,'apply',side_effect=sqlite3.OperationalError('database is locked')):result=await self.r.confirm(self.cid,self.sid,out['draft_id'],True)
        self.assertEqual(result['memory_status'],'pending');self.assertEqual(len(self.r.records(self.cid)),1)
        await self.r.apply_memory_jobs(self.cid);await self.r.apply_memory_jobs(self.cid)
        self.assertEqual(len(self.r.records(self.cid)),1);self.assertEqual(len(self.r.preference_versions(self.cid)),4);self.assertFalse(self.r.business.memory_jobs(self.r.broker,self.cid))
    async def test_failed_memory_jobs_keep_confirmation_order(self):
        first=await self.draft('只更新偏好：预算400万');second=await self.draft('只更新偏好：预算350万')
        with patch.object(self.r.memory,'apply',side_effect=ConnectionError('memory unavailable')):
            await self.r.confirm(self.cid,self.sid,first['draft_id'],True);out=await self.r.confirm(self.cid,self.sid,second['draft_id'],True)
        self.assertEqual(out['memory_status'],'pending');self.assertEqual(len(self.r.business.memory_jobs(self.r.broker,self.cid)),2)
        await asyncio.gather(self.r.apply_memory_jobs(self.cid),self.r.apply_memory_jobs(self.cid))
        self.assertEqual(self.r.profile(self.cid)['max_price_yuan'],3500000);self.assertEqual(len(self.r.preference_versions(self.cid)),2)
    async def test_confirmation_rejects_truthy_strings(self):
        out=await self.draft()
        with self.assertRaises(ValueError):await self.r.confirm(self.cid,self.sid,out['draft_id'],'false')
        self.assertFalse(self.r.records(self.cid))
    async def test_hard_filters(self):
        rows=self.r.business.search({'max_price_yuan':4000000,'bedrooms':3,'min_floor':2,'max_metro_distance_m':800,'require_elevator':True})
        self.assertTrue(rows)
        for p in rows:self.assertLessEqual(p['price_yuan'],4000000);self.assertEqual(p['bedrooms'],3);self.assertGreaterEqual(p['floor'],2);self.assertTrue(p['has_elevator']);self.assertIsNotNone(p['metro_distance_m']);self.assertLessEqual(p['metro_distance_m'],800)
    async def test_soft_metro_preference_only_sorts(self):
        rows=self.r.business.search({'prefer_near_metro':True,'limit':20});distances=[p['metro_distance_m'] if p['metro_distance_m'] is not None else 100000 for p in rows]
        self.assertEqual(distances,sorted(distances));self.assertEqual(len(rows),20)
    async def test_no_candidate_is_reported_honestly(self):
        out=await self.draft('预算100万以内，三室，不保存');self.assertIn('未找到满足条件',out['report']);self.assertNotIn('### ',out['report'])
    async def test_model_cannot_loosen_hard_constraints(self):
        self.r.tools.constraints[self.ctx.request_id]={'max_price_yuan':3000000,'bedrooms':2,'min_floor':2}
        result=await self.r.tools.execute('search_listings',{'max_price_yuan':9000000,'bedrooms':4,'min_floor':1},self.ctx)
        self.assertIn(result['status'],('ok','empty'))
        for p in result['data']:self.assertLessEqual(p['price_yuan'],3000000);self.assertEqual(p['bedrooms'],2);self.assertGreaterEqual(p['floor'],2)
    async def test_rag_community_scope(self):
        await self.r.tools.execute('search_listings',{'max_price_yuan':4000000,'bedrooms':3},self.ctx)
        keys={p['community_key'] for p in self.r.tools.candidates[self.ctx.request_id]};self.assertTrue(keys)
        key=next(iter(keys));result=await self.r.tools.execute('retrieve_area_info',{'community_keys':[key],'query':'交通和学校'},self.ctx)
        self.assertEqual(result['status'],'ok')
        for d in result['data']:self.assertTrue(d['community_key']==key or d['community_key'].startswith('region:'));self.assertTrue(d['title']);self.assertTrue(d['as_of'])
        denied=await self.r.tools.execute('retrieve_area_info',{'community_keys':['OTHER-COMMUNITY'],'query':'交通'},self.ctx);self.assertEqual(denied['error_code'],'invalid_arguments')
    async def test_real_listings_do_not_mix_demo_properties_or_documents(self):
        p={**self.r.business.properties()[0],'property_id':'R001','is_demo':False};self.r.business.put_properties([p]);rows=self.r.business.search({})
        self.assertEqual([r['property_id'] for r in rows],['R001']);self.assertEqual(self.r.knowledge.search(p['community_key'],p['region'],'交通',False),[])
    async def test_invalid_tool_arguments_do_not_execute(self):
        result=await self.r.tools.execute('search_listings',{'max_price_yuan':'400万','customer_id':self.other},self.ctx);self.assertEqual(result['error_code'],'invalid_arguments')
    async def test_model_cannot_choose_outside_candidate(self):
        result=await self.r.tools.execute('draft_recommendation',{'property_ids':['P999'],'analysis':'fake'},self.ctx);self.assertEqual(result['status'],'error')
    async def test_read_retry_is_bounded(self):
        with patch.object(self.r.business,'search',side_effect=[sqlite3.OperationalError('database is locked'),[]]) as m:result=await self.r.tools.execute('search_listings',{},self.ctx)
        self.assertEqual(m.call_count,2);self.assertEqual(result['attempts'],2)
    async def test_timeout_keeps_worker_slot_until_thread_finishes(self):
        self.r.tools.timeouts['search_listings']=.02
        with patch.object(self.r.business,'search',side_effect=lambda q:(time.sleep(.12),[])[1]):result=await self.r.tools.execute('search_listings',{},self.ctx)
        self.assertEqual(result['error_code'],'timeout');self.assertTrue(self.r.tools.workers);await self.r.tools.drain();self.assertFalse(self.r.tools.workers)
    async def test_negative_search_only_updates_memory_draft(self):
        with patch.object(self.r.business,'search',side_effect=AssertionError('must not query')):out=await self.draft('不要查询房源，只更新偏好：预算350万，三室')
        self.assertIn('客户记忆更新草稿',out['report']);self.assertEqual(self.r.profile(self.cid),{})
    async def test_followup_does_not_invent_contact(self):
        out=await self.draft('记录跟进：客户表示还想核实通勤时间');await self.r.confirm(self.cid,self.sid,out['draft_id'],True)
        self.assertEqual(self.r.followups(self.cid)[0]['content'],'记录跟进：客户表示还想核实通勤时间')
    async def test_expired_draft_is_not_committed(self):
        out=await self.draft()
        with self.r.business.connection() as con:con.execute('UPDATE drafts SET expires=0 WHERE draft_id=?',(out['draft_id'],))
        with self.assertRaises(ValueError):await self.r.confirm(self.cid,self.sid,out['draft_id'],True)
        self.assertFalse(self.r.records(self.cid))
    async def test_batch_import_rolls_back_on_collision(self):
        p=self.r.business.properties()[0];other={**p,'property_id':'R101','is_demo':False}
        with self.assertRaises(ValueError):self.r.business.put_properties([other,{**p,'is_demo':False}])
        self.assertIsNone(self.r.business.property('R101'))
    async def test_tool_loop_uses_real_host_validation(self):
        class Model(ScriptedModel):
            def __init__(self):self.n=0
            async def generate(self,messages,tools,allow_tools=True):
                self.n+=1
                if self.n==1:return {'role':'assistant','content':'','tool_calls':[{'id':'t1','type':'function','function':{'name':'search_listings','arguments':json.dumps({'max_price_yuan':9000000})}},{'id':'t2','type':'function','function':{'name':'query_customer_memory','arguments':'{"query":"偏好"}'}}]}
                return {'role':'assistant','content':'已结合候选和对应资料生成分析，学校信息仍需核验。'}
            async def aclose(self):pass
        self.r.model=Model();out=await self.r.chat('预算300万以内，两室，不保存',self.cid,self.sid,'online')
        self.assertEqual(self.r.model.n,2);self.assertIn('在线模型分析',out['report']);self.assertNotIn('总价 468万元',out['report']);self.assertIsNone(out['draft_id'])
    async def test_no_community_table(self):
        with self.r.business.connection() as con:names=[r[0].lower() for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        self.assertNotIn('community',names);self.assertNotIn('communities',names)
    async def test_online_required_without_local_fallback(self):
        with self.assertRaisesRegex(ValueError,'仅支持在线模型'):await self.r.chat('推荐房源',self.cid,self.sid,'demo')
        self.r.model=None
        with self.assertRaisesRegex(ValueError,'尚未配置在线模型'):await self.r.chat('预算400万，三室',self.cid,self.sid)
        self.assertFalse(self.r.pending(self.cid));self.assertFalse(self.r.records(self.cid))
    async def test_demo_seed_can_resume_after_partial_initialization(self):
        self.settings.data_dir.joinpath('.demo_initialized').unlink();await self.r.close();self.r=Runtime(self.settings,model=ScriptedModel())
        self.assertEqual(len(self.r.business.properties()),200);self.assertEqual(len([c for c in self.r.customers() if c['is_demo']]),10)
    async def test_vector_failure_keeps_structured_hard_preferences(self):
        self.r.memory.apply(self.r.broker,self.cid,{'max_price_yuan':3000000,'bedrooms':2},'预算300万，两室','initial')
        with patch.object(self.r.memory,'recall_light',side_effect=RuntimeError('vector failure')):out=await self.draft('推荐房源，不保存')
        self.assertIn('预算上限：300万元',out['report']);self.assertIn('向量召回暂不可用',out['report']);self.assertNotIn('总价 468万元',out['report'])
    async def test_rag_failure_keeps_structured_listing_report(self):
        with patch.object(self.r.knowledge,'search',side_effect=ConnectionError('offline')):out=await self.draft('预算400万以内，三室，不保存')
        self.assertIn('资料检索暂时失败',out['report']);self.assertIn('### ',out['report']);self.assertIsNone(out['draft_id'])
    async def test_model_empty_search_removes_earlier_candidates(self):
        class Model(ScriptedModel):
            n=0
            async def generate(self,*args,**kw):
                self.n+=1
                return {'role':'assistant','content':'','tool_calls':[{'id':'x','type':'function','function':{'name':'search_listings','arguments':'{"max_price_yuan":1000000}'}}]} if self.n==1 else {'role':'assistant','content':'当前查询没有候选。'}
            async def aclose(self):pass
        self.r.model=Model();out=await self.r.chat('预算400万以内，三室，不保存',self.cid,self.sid,'online');self.assertIn('未找到满足条件',out['report']);self.assertNotIn('### ',out['report'])
    async def test_imported_material_is_retrievable_for_its_community_only(self):
        path=Path(self.temp.name)/'source.txt';path.write_text('本资料用于核实步行通勤路线，路线A经过东侧步道。',encoding='utf-8')
        result=self.r.knowledge.import_document(path,'REAL-C','真实区域','用户提供的线路说明','2026-10-30');self.assertEqual(result['indexed'],1)
        docs=self.r.knowledge.search('REAL-C','真实区域','通勤路线',False);self.assertTrue(docs);self.assertEqual(docs[0]['source'],'用户提供的线路说明');self.assertFalse(docs[0]['is_demo'])
        self.assertFalse(self.r.knowledge.search('OTHER','其他区域','通勤路线',False))

class Parsing(unittest.TestCase):
    def test_budget_units_and_last_adjustment(self):
        for text,amount in [('预算400万',4000000),('预算四百万',4000000),('预算4000000元',4000000),('预算400万，改为350万',3500000)]:
            with self.subTest(text=text):self.assertEqual(parse_request(text)['explicit_preferences']['max_price_yuan'],amount)
    def test_clear_is_explicit(self):self.assertIsNone(parse_request('预算不限')['explicit_preferences']['max_price_yuan']);self.assertEqual(merge_preferences({'max_price_yuan':4000000},{'max_price_yuan':None}),{})
    def test_negated_first_floor_is_not_treated_as_accepting_it(self):
        self.assertEqual(parse_request('不接受一楼')['explicit_preferences']['min_floor'],2)
        self.assertIsNone(parse_request('不需要电梯')['explicit_preferences']['require_elevator'])
    def test_budget_without_prefix_and_metro_upper_bound(self):
        p=parse_request('400万以内，离地铁不超过800米')['explicit_preferences'];self.assertEqual(p['max_price_yuan'],4000000);self.assertEqual(p['max_metro_distance_m'],800)
    def test_floor_conflict_is_not_silently_relaxed(self):
        with self.assertRaises(ValueError):merge_preferences({'min_floor':2},{'max_floor':1})
    def test_skills_are_local_and_loadable(self):
        for name in CATALOGUE:self.assertIn('name: '+name,load_skill(name))
        with self.assertRaises(ValueError):load_skill('../anything')

if __name__=='__main__':unittest.main()



