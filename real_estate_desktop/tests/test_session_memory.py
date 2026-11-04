import asyncio,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from real_estate_desktop.runtime import Runtime
from real_estate_desktop.config import Settings
from real_estate_desktop.test_support import ScriptedModel
from real_estate_desktop.tools import Context

class SessionMemory(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration=3
        self.temp=tempfile.TemporaryDirectory();self.settings=Settings(Path(self.temp.name));self.r=Runtime(self.settings,model=ScriptedModel())
        self.cid=self.r.create_customer('会话缓存客户')['customer_id'];self.sid=self.r.new_session(self.cid)
        self.r.memory.apply(self.r.broker,self.cid,{'max_price_yuan':4000000,'bedrooms':3},'客户已确认要求','seed')
    async def asyncTearDown(self):await self.r.close();self.temp.cleanup()
    def ctx(self,sid=None,cid=None,request='request'):return Context(self.r.broker,cid or self.cid,sid or self.sid,request)
    async def read(self,ctx,refresh=False):return await self.r.tools.execute('query_customer_memory',{'query':'客户偏好','refresh':refresh},ctx)
    async def test_core_memory_is_cached_and_episodic_recall_stays_model_driven(self):
        with patch.object(self.r.memory,'recall_light',wraps=self.r.memory.recall_light) as light, \
             patch.object(self.r.memory,'recall',wraps=self.r.memory.recall) as full:
            await self.r.chat('预算400万，三室，不保存',self.cid,self.sid)
            await self.r.chat('预算改为350万，三室，不保存',self.cid,self.sid)
            self.assertEqual(light.call_count,1)
            self.assertEqual(full.call_count,0)
    async def test_cache_is_customer_and_conversation_scoped(self):
        other=self.r.create_customer('其他客户')['customer_id'];sid2=self.r.new_session(other)
        self.r.memory.apply(self.r.broker,other,{'max_price_yuan':2500000},'其他客户要求','other')
        first=await self.read(self.ctx());second=await self.read(self.ctx(sid2,other))
        self.assertEqual(first['data']['preferences']['max_price_yuan'],4000000);self.assertEqual(second['data']['preferences']['max_price_yuan'],2500000)
        forged=await self.read(self.ctx(sid2,self.cid));self.assertEqual(forged['error_code'],'forbidden')
    async def test_identical_concurrent_session_reads_are_coalesced(self):
        original=self.r.memory.recall
        def load(*args):time.sleep(.05);return original(*args)
        with patch.object(self.r.memory,'recall',side_effect=load) as recall:
            first,second=await asyncio.gather(self.read(self.ctx(request='a')),self.read(self.ctx(request='b')))
        self.assertEqual(recall.call_count,1);self.assertEqual(first['data']['preferences'],second['data']['preferences'])
    async def test_confirmed_update_refreshes_all_open_sessions_without_vector_recall(self):
        sid2=self.r.new_session(self.cid)
        with patch.object(self.r.memory,'recall',wraps=self.r.memory.recall) as recall:
            await self.read(self.ctx());await self.read(self.ctx(sid2))
            out=await self.r.chat('预算改为350万，三室',self.cid,self.sid);await self.r.confirm(self.cid,self.sid,out['draft_id'],True)
            first=await self.read(self.ctx());second=await self.read(self.ctx(sid2))
            self.assertGreaterEqual(recall.call_count,2);self.assertEqual(first['data']['preferences']['max_price_yuan'],3500000);self.assertEqual(second['data']['preferences']['max_price_yuan'],3500000)
            self.assertEqual(second['data']['evidence'],[])
    async def test_old_conversation_cannot_restore_a_superseded_confirmed_budget(self):
        await self.r.chat('预算400万，三室，不保存',self.cid,self.sid);sid2=self.r.new_session(self.cid)
        out=await self.r.chat('预算改为350万，三室',self.cid,sid2);await self.r.confirm(self.cid,sid2,out['draft_id'],True)
        class Capture(ScriptedModel):
            async def plan_search(self,context):self.context=context;return await super().plan_search(context)
        self.r.model=Capture();await self.r.chat('继续推荐，不保存',self.cid,self.sid)
        self.assertEqual(self.r.model.context['current_preferences']['max_price_yuan'],3500000)
    async def test_unconfirmed_changes_do_not_mutate_cached_long_term_preferences(self):
        await self.r.chat('预算改为350万，三室，不保存',self.cid,self.sid)
        out=await self.read(self.ctx());self.assertEqual(out['data']['preferences']['max_price_yuan'],4000000)
        out['data']['preferences']['max_price_yuan']=1
        again=await self.read(self.ctx());self.assertEqual(again['data']['preferences']['max_price_yuan'],4000000)
    async def test_explicit_refresh_can_read_external_changes(self):
        with patch.object(self.r.memory,'recall',wraps=self.r.memory.recall) as recall:
            await self.read(self.ctx());self.r.memory.apply(self.r.broker,self.cid,{'max_price_yuan':2800000},'外部已确认更新','external')
            cached=await self.read(self.ctx());self.assertEqual(cached['data']['preferences']['max_price_yuan'],4000000)
            out=await self.r.chat('重新读取我的长期记忆',self.cid,self.sid)
            self.assertIn('280万元',out['report']);self.assertIsNone(out['draft_id'])
    async def test_vector_failure_uses_cached_structured_profile_in_later_turns(self):
        with patch.object(self.r.memory,'recall_light',side_effect=RuntimeError('vector unavailable')) as recall:
            first=await self.r.chat('推荐房源，不保存',self.cid,self.sid);second=await self.r.chat('继续推荐，不保存',self.cid,self.sid)
        self.assertGreaterEqual(recall.call_count,1);self.assertIn('400万元',first['report']);self.assertIn('400万元',second['report'])
    async def test_application_restart_rebuilds_the_session_snapshot(self):
        await self.read(self.ctx());await self.r.close()
        # Redis deliberately keeps the snapshot across a restart; a cold cache
        # (fresh machine, TTL expiry, flushed database) must rebuild it.
        self.r=Runtime(self.settings,model=ScriptedModel());self.r.memory_contexts.invalidate_all()
        with patch.object(self.r.memory,'recall',wraps=self.r.memory.recall) as recall:out=await self.read(self.ctx())
        self.assertEqual(recall.call_count,1);self.assertEqual(out['data']['context_origin'],'retrieved')
    async def test_warm_redis_cache_survives_restart_without_vector_recall(self):
        if self.r.cache.backend != 'redis':
            self.skipTest('Redis is unavailable; cross-runtime cache persistence requires Redis')
        await self.read(self.ctx());await self.r.close()
        self.r=Runtime(self.settings,model=ScriptedModel())
        with patch.object(self.r.memory,'recall',wraps=self.r.memory.recall) as recall:out=await self.read(self.ctx())
        self.assertEqual(recall.call_count,0)
        self.assertEqual(out['data']['context_origin'],'session_cache')
        self.assertEqual(out['data']['preferences']['max_price_yuan'],4000000)

