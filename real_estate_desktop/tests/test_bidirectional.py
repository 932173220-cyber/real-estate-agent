import asyncio,json,re,tempfile,threading,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from real_estate_desktop.runtime import Runtime
from real_estate_desktop.config import Settings
from real_estate_desktop.test_support import ScriptedModel
from real_estate_desktop.tools import Context

class Bidirectional(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration=3
        self.temp=tempfile.TemporaryDirectory();self.r=Runtime(Settings(Path(self.temp.name)),model=ScriptedModel())
        self.cid=self.r.create_customer('双向检索客户')['customer_id'];self.sid=self.r.new_session(self.cid)
    async def asyncTearDown(self):await self.r.close();self.temp.cleanup()
    async def chat(self,text):return await self.r.chat(text,self.cid,self.sid)
    def events(self):
        path=self.r.settings.data_dir/'logs/tools.jsonl'
        return [json.loads(line) for line in path.read_text('utf-8').splitlines()]
    def ids(self,report):return re.findall(r'^### ([A-Za-z0-9_-]+) · ',report,re.M)
    async def test_amenities_only_retrieve_communities_before_any_listing(self):
        with patch.object(self.r.business,'search',side_effect=AssertionError('must clarify before filtering houses')):out=await self.chat('希望有小学和医院，买菜方便，先看看小区')
        self.assertEqual(out['search_route'],'community_first');self.assertIn('### 1.',out['report']);self.assertIsNone(out['draft_id']);self.assertFalse(self.ids(out['report']))
        starts=[e['tool'] for e in self.events() if e['event']=='start'];self.assertIn('search_communities',starts);self.assertNotIn('search_listings',starts)
    async def test_amenities_ask_for_missing_budget_and_rooms_then_followup(self):
        first=await self.chat('我想附近有小学和医院，买菜方便');self.assertIn('预算上限',first['report']);self.assertIn('希望几室',first['report']);self.assertFalse(self.r.profile(self.cid))
        out=await self.chat('预算400万以内，三室，不要一楼，不保存');self.assertEqual(out['search_route'],'parallel');self.assertTrue(self.ids(out['report']))
        for id in self.ids(out['report']):p=self.r.business.property(id);self.assertLessEqual(p['price_yuan'],4000000);self.assertEqual(p['bedrooms'],3);self.assertGreater(p['floor'],1)
    async def test_hard_conditions_only_do_not_call_global_rag(self):
        with patch.object(self.r.knowledge,'discover',side_effect=AssertionError('not needed')):out=await self.chat('预算400万以内，三室，不要一楼，不保存')
        self.assertEqual(out['search_route'],'listing_first');self.assertTrue(self.ids(out['report']));self.assertIn('小区与区域资料',out['report'])
    async def test_parallel_reads_overlap_and_results_are_intersected(self):
        barrier=threading.Barrier(2);original_db=self.r.business.search;original_rag=self.r.knowledge.discover
        waited={'db':False,'rag':False}
        def db(*args,**kw):
            if not waited['db']:waited['db']=True;barrier.wait(timeout=3)
            return original_db(*args,**kw)
        def rag(*args,**kw):
            if not waited['rag']:waited['rag']=True;barrier.wait(timeout=3)
            return original_rag(*args,**kw)
        with patch.object(self.r.business,'search',side_effect=db),patch.object(self.r.knowledge,'discover',side_effect=rag):out=await self.chat('预算400万以内，三室，小学和医院配套，不保存')
        self.assertEqual(out['search_route'],'parallel');self.assertTrue(self.ids(out['report']));self.assertTrue(any(e['event']=='parallel_intersection' for e in self.events()))
    async def test_named_community_filters_its_properties_only(self):
        out=await self.chat('看看澄湾雅苑，预算400万以内，三室，不要一楼，不保存')
        self.assertEqual(out['search_route'],'community_listings');ids=self.ids(out['report']);self.assertTrue(ids);self.assertTrue(all(self.r.business.property(id)['community_name']=='澄湾雅苑' for id in ids))
    async def test_property_lookup_returns_community_even_over_budget_without_recommending(self):
        self.r.memory.apply(self.r.broker,self.cid,{'max_price_yuan':3000000,'bedrooms':2},'客户要求','seed')
        out=await self.chat('P013这套房子所属小区的配套怎么样？');self.assertEqual(out['search_route'],'property_details');self.assertEqual(self.ids(out['report']),['P013'])
        self.assertIn('澄湾雅苑',out['report']);self.assertIn('总价超过预算',out['report']);self.assertIn('户型不符',out['report']);self.assertIsNone(out['draft_id']);self.assertFalse(self.r.records(self.cid))
    async def test_unknown_property_never_falls_back_to_other_houses(self):
        with patch.object(self.r.business,'search',side_effect=AssertionError('must not substitute')):out=await self.chat('P999所属的小区怎么样？')
        self.assertIn('未找到',out['report']);self.assertFalse(self.ids(out['report']));self.assertIsNone(out['draft_id'])
    async def test_unknown_community_never_falls_back_to_other_communities(self):
        out=await self.chat('看看晨曦花园的房源');self.assertIn('未找到',out['report']);self.assertFalse(self.ids(out['report']));self.assertIsNone(out['draft_id'])
    async def test_specific_unknown_school_is_not_replaced_by_generic_school_mentions(self):
        out=await self.chat('我想找不存在实验小学附近的房子，预算400万，三室，不保存')
        self.assertIn('暂未检索到',out['report']);self.assertFalse(self.ids(out['report']));self.assertIsNone(out['draft_id'])
    async def test_model_cannot_shorten_a_specific_school_to_a_generic_suffix(self):
        class Router(ScriptedModel):
            async def plan_search(self,context):return {'route':'parallel','query':'实验小学','exact_terms':['实验小学']}
        self.r.model=Router();out=await self.chat('预算400万，三室，必须有不存在实验小学的配套，不保存')
        self.assertIn('暂未检索到',out['report']);self.assertFalse(self.ids(out['report']));self.assertIsNone(out['draft_id'])
    async def test_model_cannot_drop_named_school_and_skip_its_retrieval(self):
        class Router(ScriptedModel):
            async def plan_search(self,context):return {'route':'listing_first','query':'三室房源','exact_terms':[]}
        self.r.model=Router();out=await self.chat('预算400万，三室，必须有不存在实验小学的配套，不保存')
        self.assertEqual(out['search_route'],'parallel');self.assertIn('暂未检索到',out['report']);self.assertFalse(self.ids(out['report']))
    async def test_explicitly_withdrawn_school_does_not_block_later_search(self):
        await self.chat('预算400万，三室，希望有不存在实验小学的配套，不保存')
        out=await self.chat('不考虑学校，预算400万，三室，不保存')
        self.assertTrue(self.ids(out['report']))
    async def test_matching_community_without_houses_is_reported_as_no_intersection(self):
        self.r.knowledge.import_text(uuid.uuid4().hex,title='星河苑 · 对话资料',community_key='NO-HOUSE',region='新城区',source='',as_of='2026-10-30',text='星河苑东门旁有星河实验小学，附近有超市，步行购物方便。')
        out=await self.chat('想找星河实验小学附近的房子，预算400万以内，三室')
        self.assertIn('没有同时满足',out['report']);self.assertFalse(self.ids(out['report']));self.assertIn('星河苑',out['report']);self.assertIsNone(out['draft_id'])
    async def test_empty_intersection_never_relaxes_budget(self):
        out=await self.chat('预算10万以内，三室，小学和医院附近')
        self.assertIn('没有同时满足',out['report']);self.assertFalse(self.ids(out['report']));self.assertIsNone(out['draft_id']);self.assertFalse(self.r.pending(self.cid))
    async def test_parallel_rag_failure_does_not_approve_unchecked_houses(self):
        with patch.object(self.r.knowledge,'discover',side_effect=ConnectionError('unavailable')):out=await self.chat('预算400万，三室，小学医院配套')
        self.assertIn('暂不可用',out['report']);self.assertFalse(self.ids(out['report']));self.assertIsNone(out['draft_id'])
    async def test_lookup_tool_cannot_pollute_recommendation_candidates(self):
        ctx=Context(self.r.broker,self.cid,self.sid,'request-guard');self.r.tools.constraints[ctx.request_id]={'max_price_yuan':3000000,'bedrooms':2}
        before=await self.r.tools.execute('search_listings',{},ctx);ids={p['property_id'] for p in before['data']}
        await self.r.tools.execute('get_property_details',{'property_id':'P013'},ctx)
        self.assertEqual({p['property_id'] for p in self.r.tools.candidates[ctx.request_id]},ids)
        out=await self.r.tools.execute('draft_recommendation',{'property_ids':['P013'],'analysis':'推荐'},ctx);self.assertEqual(out['status'],'error')
    async def test_area_orientation_and_finish_constraints_are_enforced(self):
        out=await self.chat('预算400万，三室，面积100平以上，120平以内，朝南，要求精装，不保存')
        self.assertTrue(self.ids(out['report']))
        for id in self.ids(out['report']):
            p=self.r.business.property(id);self.assertGreaterEqual(p['area_sqm'],100);self.assertLessEqual(p['area_sqm'],120);self.assertIn('南',p['orientation']);self.assertEqual(p['decoration'],'精装')
    async def test_explicit_memory_nodes_and_confirm_only_update(self):
        out=await self.chat('预算400万以内，三室，不要一楼');self.assertFalse(self.r.profile(self.cid))
        self.assertIn('recall_memory',self.r.workflow.graph.nodes);self.assertIn('update_memory',self.r.workflow.graph.nodes)
        await self.r.confirm(self.cid,self.sid,out['draft_id'],True)
        self.assertEqual(self.r.profile(self.cid)['max_price_yuan'],4000000)
        events=[r['event'] for r in self.events() if r['tool']=='workflow'];self.assertLess(events.index('recall_memory'),events.index('resolved'));self.assertIn('update_memory',events)
    async def test_numbered_community_followup_uses_previous_candidates(self):
        first=await self.chat('先看看有小学、医院配套的小区');name=re.search(r'^### 1\. (.+?) ｜',first['report'],re.M)[1]
        out=await self.chat('就第一个，预算400万以内，三室，不保存');self.assertEqual(out['search_route'],'community_listings');self.assertTrue(self.ids(out['report']))
        self.assertTrue(all(self.r.business.property(id)['community_name']==name for id in self.ids(out['report'])))
    async def test_search_context_does_not_cross_customer(self):
        await self.chat('小学和医院配套，先看看小区');other=self.r.create_customer('另一位客户')['customer_id'];sid=self.r.new_session(other)
        out=await self.r.chat('预算400万以内，三室，不保存',other,sid);self.assertEqual(out['search_route'],'listing_first')
    async def test_empty_community_selector_is_not_treated_as_unrestricted(self):
        self.assertEqual(self.r.business.search({'community_keys':[]}),[])
    async def test_model_cannot_turn_hard_only_request_into_a_lookup_question(self):
        class Router(ScriptedModel):
            async def plan_search(self,context):
                host=context['_host_plan']
                return {'route':'property_details','query':host['query'],'questions':[],'exact_terms':[]}
        self.r.model=Router()
        out=await self.chat('预算400万以内，三室，不要一楼，不保存')
        self.assertEqual(out['search_route'],'listing_first')
        self.assertTrue(self.ids(out['report']))
    async def test_model_cannot_redirect_hard_only_request_to_community_search(self):
        class Router(ScriptedModel):
            async def plan_search(self,context):
                host=context['_host_plan']
                return {'route':'community_first','query':host['query'],'questions':[],'exact_terms':[]}
        self.r.model=Router()
        out=await self.chat('预算400万以内，三室，不要一楼，不保存')
        self.assertEqual(out['search_route'],'listing_first')
        self.assertTrue(self.ids(out['report']))
    async def test_model_cannot_drop_a_detected_amenity_by_downgrading_the_route(self):
        class Router(ScriptedModel):
            async def plan_search(self,context):
                host=context['_host_plan']
                return {'route':'listing_first','query':host['query'],'questions':[],'exact_terms':[]}
        self.r.model=Router()
        out=await self.chat('预算400万以内，三室，必须有小学，不保存')
        self.assertEqual(out['search_route'],'parallel')
    async def test_soft_amenity_wording_does_not_upgrade_listing_search(self):
        out=await self.chat('预算400万以内，三室，最好有小学，不保存')
        self.assertEqual(out['search_route'],'listing_first');self.assertTrue(self.ids(out['report']))
        starts=[e['tool'] for e in self.events() if e['event']=='start'];self.assertNotIn('search_communities',starts)
        out=await self.chat('预算400万，三室，尽量离地铁近，希望附近有医院，不保存')
        self.assertEqual(out['search_route'],'listing_first')
    async def test_hard_school_district_wording_still_upgrades(self):
        out=await self.chat('预算400万以内，三室，要学区，不保存')
        self.assertEqual(out['search_route'],'parallel')
    async def test_model_router_receives_memory_and_can_understand_beyond_keyword_rules(self):
        self.r.memory.apply(self.r.broker,self.cid,{'max_price_yuan':4000000,'bedrooms':3},'客户偏好','init')
        class Router(ScriptedModel):
            async def plan_search(self,context):
                self.context=context
                return {'route':'community_first','query':'医疗门诊，方便复诊','questions':[]}
        self.r.model=Router();text='想方便带家人复诊，省心一点，不保存';out=await self.chat(text)
        self.assertEqual(self.r.model.context['_host_plan']['route'],'listing_first');self.assertEqual(out['search_route'],'community_first')
        self.assertEqual(self.r.model.context['current_message'],text);self.assertEqual(self.r.model.context['customer_memory']['preferences']['max_price_yuan'],4000000)
        self.assertEqual(self.r.model.context['conversation'][-1]['content'],text);self.assertTrue(self.ids(out['report']))
    async def test_current_constraints_override_memory_before_model_routing(self):
        self.r.memory.apply(self.r.broker,self.cid,{'max_price_yuan':4000000,'bedrooms':2},'旧偏好','init')
        class Router(ScriptedModel):
            async def plan_search(self,context):self.context=context;return await super().plan_search(context)
        self.r.model=Router();out=await self.chat('预算改为350万，三室，不保存')
        self.assertEqual(self.r.model.context['current_preferences']['max_price_yuan'],3500000);self.assertEqual(self.r.model.context['current_preferences']['bedrooms'],3)
        self.assertEqual(self.r.model.context['customer_memory']['preferences']['bedrooms'],2)
        self.assertTrue(all(self.r.business.property(id)['price_yuan']<=3500000 and self.r.business.property(id)['bedrooms']==3 for id in self.ids(out['report'])))
    async def test_property_description_does_not_redefine_customer_budget_or_rooms(self):
        self.r.memory.apply(self.r.broker,self.cid,{'max_price_yuan':4000000,'bedrooms':3},'客户要求','init')
        out=await self.chat('P013这套总价468万，四室，周边配套如何？');self.assertIn('总价超过预算',out['report']);self.assertIn('户型不符',out['report'])
        self.assertEqual(self.r.profile(self.cid),{'max_price_yuan':4000000,'bedrooms':3})
    async def test_confirmed_amenity_preferences_reach_next_conversation_router(self):
        class Router(ScriptedModel):
            async def plan_search(self,context):
                self.context=context
                if '我希望有小学和医院' in context['current_message']:
                    result=await super().plan_search(context);return {**result,'preference_topics':['小学','医疗'],'preference_quote':'我希望有小学和医院'}
                return {'route':'community_first','query':'小学 医疗','questions':[],'exact_terms':[]}
        self.r.model=Router();out=await self.chat('预算400万，三室，我希望有小学和医院')
        self.assertFalse(self.r.profile(self.cid));await self.r.confirm(self.cid,self.sid,out['draft_id'],True)
        self.assertEqual(self.r.profile(self.cid)['amenity_preferences'],['小学','医疗'])
        self.sid=self.r.new_session(self.cid);new=await self.chat('按我的配套偏好继续找，不保存')
        self.assertEqual(self.r.model.context['customer_memory']['preferences']['amenity_preferences'],['小学','医疗']);self.assertTrue(self.ids(new['report']))
    async def test_ungrounded_amenity_memory_proposal_is_not_saved(self):
        class Router(ScriptedModel):
            async def plan_search(self,context):
                result=await super().plan_search(context);return {**result,'preference_topics':['银河学区'],'preference_quote':'配套方便'}
        self.r.model=Router();out=await self.chat('预算400万，三室，配套方便')
        await self.r.confirm(self.cid,self.sid,out['draft_id'],True);self.assertNotIn('amenity_preferences',self.r.profile(self.cid))

if __name__=='__main__':unittest.main()


