from __future__ import annotations
import asyncio,json,uuid
from .config import load_settings
from .store import BusinessStore
from .memory import CustomerMemory
from .knowledge import CommunityKnowledge
from .tools import ToolService,Context
from .workflow import PropertyWorkflow
from .model_client import ChatModelClient
from .demo import seed_demo
from .sample_labels import migrate_sample_labels
from .catalog import CatalogService,looks_like_intake,looks_like_authority_request
from .session_memory import SessionMemoryContexts
from .embeddings import load_embedder
from .cache import make_cache

class Runtime:
    def __init__(self,settings=None,*,demo=True,model=None):
        self.settings=settings or load_settings();self.business=BusinessStore(self.settings.business_db)
        self.embedder=load_embedder(self.settings)
        self.memory=CustomerMemory(self.settings.memory_db,embedder=self.embedder)
        self.knowledge=CommunityKnowledge(self.settings.knowledge_db,self.settings.knowledge_dir,embedder=self.embedder,
            chroma_dir=self.settings.chroma_dir,dense_top_k=self.settings.dense_top_k,lexical_top_k=self.settings.lexical_top_k,
            rrf_k=self.settings.rrf_k,top_k=self.settings.retrieval_top_k)
        if demo:seed_demo(self.settings,self.business,self.memory,self.knowledge)
        migrate_sample_labels(self.settings,self.business,self.memory,self.knowledge)
        self.cache=make_cache(self.settings)
        self.memory_contexts=SessionMemoryContexts(self.cache,ttl=self.settings.cache_ttl,history_turns=self.settings.history_turns)
        self.tools=ToolService(self.business,self.memory,self.knowledge,self.settings.data_dir/'logs/tools.jsonl',memory_contexts=self.memory_contexts)
        self.model=model or (ChatModelClient(self.settings) if self.settings.chat_api_key and self.settings.chat_model else None)
        self.workflow=PropertyWorkflow(self);self.memory_locks={};self.catalog=CatalogService(self);self.background=set()
        self.memory.on_change=self.invalidate_profile
    def invalidate_profile(self,broker,cid):
        self.cache.invalidate(self.cache.k('profile',broker,cid))
        # A manual memory edit must not be masked by an earlier same-request tool result.
        self.tools.call_cache.clear()
    @property
    def broker(self):return self.settings.broker_id
    def customers(self):return self.business.customers(self.broker)
    def create_customer(self,name,notes=''):return self.business.create_customer(self.broker,name,notes)
    def customer(self,cid):return self.business.customer(self.broker,cid)
    def new_session(self,cid):return self.business.create_session(self.broker,cid)
    def sessions(self,cid):return self.business.sessions(self.broker,cid)
    def history(self,cid,sid):return self.business.history(self.broker,cid,sid)
    def profile(self,cid):
        self.customer(cid)
        key=self.cache.k('profile',self.broker,cid)
        cached=self.cache.get_json(key)
        if cached is not None:return cached
        value=self.memory.profile(self.broker,cid)
        self.cache.set_json(key,value,min(self.settings.cache_ttl,300))
        return value
    def preference_versions(self,cid):self.customer(cid);return self.memory.versions(self.broker,cid)
    def memory_records(self,cid,*,history=False):self.customer(cid);return self.memory.typed_context(self.broker,cid,history=history)
    def memory_changes(self,cid,limit=200):self.customer(cid);return self.memory.changes(self.broker,cid,limit)
    def update_preference(self,cid,key,value):
        self.customer(cid);result=self.memory.update_preference(self.broker,cid,key,value)
        self.memory_contexts.invalidate_customer(self.broker,cid);return result
    def update_memory_record(self,cid,record_id,*,content=None,value=None):
        self.customer(cid);result=self.memory.update_record(self.broker,cid,record_id,content=content,value=value)
        self.memory_contexts.invalidate_customer(self.broker,cid);return result
    def delete_memory_record(self,cid,record_id):
        self.customer(cid);result=self.memory.delete_record(self.broker,cid,record_id)
        self.memory_contexts.invalidate_customer(self.broker,cid);return result
    def clear_memory(self,cid):
        self.customer(cid);result=self.memory.clear(self.broker,cid)
        self.memory_contexts.invalidate_customer(self.broker,cid);return result
    async def undo_last_write(self,cid):
        self.customer(cid)
        batch=await self.tools.sync(self.business.undo_last,self.broker,cid)
        rolled=await self.tools.sync(self.memory.undo_batch,self.broker,cid,batch['draft_id'])
        typed=await self.tools.sync(self.memory.undo_typed,self.broker,cid,batch['draft_id'],batch['session_id'])
        self.memory_contexts.invalidate_customer(self.broker,cid)
        return {**batch,**rolled,**typed}
    def memory_caller(self):
        loop=asyncio.get_running_loop();model=self.model
        def call(prompt,data,schema):
            if model is None or not hasattr(model,'memory_json'):raise ValueError('当前模型不支持长期记忆结构化调用。')
            future=asyncio.run_coroutine_threadsafe(model.memory_json(prompt,data,schema),loop)
            try:return future.result(timeout=125)
            except BaseException:future.cancel();raise
        return call
    async def extract(self,prompt,data,schema):
        """One structured JSON extraction through the configured model; callers validate it."""
        model=self.model
        if model is None:raise ValueError('尚未配置在线模型，请在配置文件中填写模型名称和API密钥。')
        method=getattr(model,'extract_json',None) or getattr(model,'memory_json',None)
        if method is None:raise ValueError('当前模型不支持结构化提取。')
        try:
            async with asyncio.timeout(150):return await method(prompt,data,schema)
        except TimeoutError as error:raise ValueError('结构化提取超时，请稍后重试。') from error
    async def prepare_memory(self,ctx,text,report):
        source=self.business.message(ctx.broker_id,ctx.customer_id,ctx.session_id,ctx.request_id+'-user')
        if source['content']!=text:raise ValueError('记忆提取必须使用已保存的原始消息。')
        return await self.tools.sync(self.memory.prepare_typed,ctx.broker_id,ctx.customer_id,ctx.session_id,source,report,
            self.business.history(ctx.broker_id,ctx.customer_id,ctx.session_id)[-self.settings.history_turns*2:],self.memory_caller())
    async def chat(self,text,cid,sid,mode='online'):
        if mode!='online':raise ValueError('当前仅支持在线模型。')
        if not text.strip() or len(text)>6000:raise ValueError('请输入1至6000字的客户需求。')
        self.business.session(self.broker,cid,sid)
        # A new turn must see the previous turn's auto-memory; otherwise a delayed
        # background write can make the next turn fall back to an older budget.
        if self.background:await asyncio.gather(*tuple(self.background),return_exceptions=True)
        request=uuid.uuid4().hex;ctx=Context(self.broker,cid,sid,request)
        if looks_like_intake(text):
            return {'report':'这是房源/小区资料录入，请通过“对话录入”窗口粘贴信息。该内容尚未进入客户对话或长期偏好。','draft_id':None,'catalog_redirect':text,'customer_id':cid,'session_id':sid}
        if looks_like_authority_request(text):
            # Scope changes are decided by the host, never by the message text.
            report=('只能处理当前这位客户的数据，不能切换、查看或修改其他客户的记录，也不能删除客户数据。'
                    '如果确实需要为另一位客户服务，请在客户选择中切换。')
            self.business.append(self.broker,cid,sid,'user',text,request+'-user')
            self.business.append(self.broker,cid,sid,'assistant',report,request+'-reply')
            return {'report':report,'draft_id':None,'customer_id':cid,'session_id':sid,'authority_refused':True}
        self.business.append(self.broker,cid,sid,'user',text,request+'-user')
        try:
            async with asyncio.timeout(360):result=await self.workflow.start(text,ctx,mode)
            result['trace']=self.tools.trace(ctx)
            self.business.append(self.broker,cid,sid,'assistant',result['report'],request+'-reply')
            if (getattr(self.settings,'report_mode','auto')=='explicit' and result.get('draft_id') is None
                    and not result.get('catalog_redirect') and not result.get('authority_refused')):
                # Memory is learned every turn, after the answer is already produced,
                # so the broker never has to confirm a memory write.
                task=asyncio.create_task(self._remember(ctx,text,result['report']))
                self.background.add(task);task.add_done_callback(self.background.discard)
            return result
        except Exception as e:
            from openai import RateLimitError
            if isinstance(e,RateLimitError):raise ValueError('模型接口额度或请求限制导致在线分析不可用，请检查账户额度后重试。') from e
            if isinstance(e,TimeoutError):raise ValueError('这次处理超过等待时间，请缩短需求后重试。') from e
            raise
        finally:
            self.tools.constraints.pop(request,None);self.tools.candidates.pop(request,None)
            for mapping in (self.tools.communities,self.tools.community_scopes,self.tools.exact_terms,self.tools.lookups,self.tools.active,self.tools.traces,self.tools.thoughts):mapping.pop(request,None)
            self.tools.clear_request(request)
            self.tools.inspections.discard(request)
    async def _remember(self,ctx,text,report):
        """Best-effort: learn from this turn without blocking the answer or failing it."""
        from .search_planning import customer_preferences
        preferences=customer_preferences(text)
        if preferences:
            try:
                await self.tools.sync(self.memory.apply,self.broker,ctx.customer_id,preferences,text,ctx.request_id)
                await self._refresh_memory_cache(ctx.customer_id)
            except Exception:
                pass
        try:
            plan=await self.prepare_memory(ctx,text,report)
            if plan.get('candidates') or plan.get('requests'):
                await self.tools.sync(self.memory.commit_typed,self.broker,ctx.customer_id,plan,'auto-'+ctx.request_id,self.memory_caller())
                await self._refresh_memory_cache(ctx.customer_id)
        except Exception:
            pass
        try:await self.apply_memory_jobs(ctx.customer_id)
        except Exception:pass
    async def _refresh_memory_cache(self,cid):
        profile=await self.tools.sync(self.memory.profile,self.broker,cid)
        versions=await self.tools.sync(self.memory.active_versions,self.broker,cid)
        typed=await self.tools.sync(self.memory.typed_context,self.broker,cid)
        await self.memory_contexts.update_customer(self.broker,cid,profile,versions,typed)
    async def apply_memory_jobs(self,cid):
        self.customer(cid)
        async with self.memory_locks.setdefault(cid,asyncio.Lock()):
            pending=self.business.memory_jobs(self.broker,cid);retried=0;clarifications=0
            for job in pending:
                payload=json.loads(job['payload']);retried+=1
                try:
                    decisions=await self.tools.sync(self.memory.commit_typed,self.broker,cid,payload['memory_plan'],job['job_id'],self.memory_caller()) if 'memory_plan' in payload else {}
                    clarifications+=decisions.get('pending',0)
                    excluded=set(decisions.get('pending_attributes',[]))
                    if decisions.get('block_projection') or decisions.get('pending') and not excluded:excluded.update(payload['preferences'])
                    profile=await self.tools.sync(self.memory.apply,self.broker,cid,{k:v for k,v in payload['preferences'].items() if k not in excluded},payload['source'],job['job_id'])
                    versions=await self.tools.sync(self.memory.active_versions,self.broker,cid)
                    typed=await self.tools.sync(self.memory.typed_context,self.broker,cid)
                    await self.memory_contexts.update_customer(self.broker,cid,profile,versions,typed)
                    self.business.finish_memory(job['job_id'])
                except Exception as e:
                    # Preserve confirmation order: an old failed fact cannot overwrite a newer one later.
                    self.business.finish_memory(job['job_id'],type(e).__name__);break
            return {'pending':len(self.business.memory_jobs(self.broker,cid)),'retried':retried,'clarifications':clarifications}
    async def commit_draft(self,ctx,id,approved,*,sync_memory=True):
        result=await self.tools.sync(self.business.commit,ctx.broker_id,ctx.customer_id,ctx.session_id,id,approved)
        if result['status']=='saved' and sync_memory:
            jobs=await self.apply_memory_jobs(ctx.customer_id);result['memory_status']='pending' if jobs['pending'] else 'needs_clarification' if jobs['clarifications'] else 'ok'
        return result
    async def confirm(self,cid,sid,id,approved):
        if type(approved) is not bool:raise ValueError('确认结果必须是明确的是或否。')
        self.business.session(self.broker,cid,sid)
        return await self.workflow.confirm(Context(self.broker,cid,sid,uuid.uuid4().hex),id,approved)
    def pending(self,cid,sid=None):return self.business.pending(self.broker,cid,sid)
    def records(self,cid):return self.business.records(self.broker,cid)
    def followups(self,cid):return self.business.followups(self.broker,cid)
    async def close(self):
        if self.background:
            try:await asyncio.wait_for(asyncio.gather(*self.background,return_exceptions=True),timeout=30)
            except Exception:pass
        await self.workflow.close();await self.tools.drain()
        if self.model:await self.model.aclose()
        self.memory_contexts.clear()
        self.cache.close()
        self.knowledge.close()
        self.memory.close()
