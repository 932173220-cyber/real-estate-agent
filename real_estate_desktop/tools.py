from __future__ import annotations
import asyncio,copy,json,sqlite3,time
from dataclasses import dataclass
from pydantic import ValidationError
from .domain import ListingQuery,AreaArgs,MemoryArgs,SkillArgs,DraftArgs,CommunitySearchArgs,PropertyDetailsArgs,ThinkArgs
from .skill_registry import load_skill, registry
from .search_planning import mismatches
from .session_memory import SessionMemoryContexts

@dataclass(frozen=True)
class Context:
    broker_id:str
    customer_id:str
    session_id:str
    request_id:str

SKILLS=registry.legacy_catalogue()

class ToolService:
    READ_ONLY={'search_listings','search_communities','get_property_details','retrieve_area_info','query_customer_memory'}
    schemas={'think':ThinkArgs,'search_listings':ListingQuery,'search_communities':CommunitySearchArgs,'get_property_details':PropertyDetailsArgs,'retrieve_area_info':AreaArgs,'query_customer_memory':MemoryArgs,'load_skill':SkillArgs,'draft_recommendation':DraftArgs}
    def __init__(self,business,memory,knowledge,log_path,timeout=8,workers=4,memory_contexts=None):
        self.business,self.memory,self.knowledge=business,memory,knowledge;self.log_path=log_path
        self.timeouts={'search_listings':6,'search_communities':20,'retrieve_area_info':20,'query_customer_memory':10,'get_property_details':8,'draft_recommendation':5,'load_skill':5}
        self.memory_contexts=memory_contexts or SessionMemoryContexts()
        log_path.parent.mkdir(parents=True,exist_ok=True);self.timeout=timeout;self.slots=asyncio.Semaphore(workers);self.workers=set();self.constraints={};self.candidates={};self.communities={};self.community_scopes={};self.exact_terms={};self.inspections=set();self.lookups={};self.active={};self.traces={};self.thoughts={};self.call_cache={}
    def trace(self,ctx):return [dict(step) for step in self.traces.get(ctx.request_id,[])]
    def activate(self,ctx,*names):
        """Expose a skill's tool schemas to the model for this request only."""
        bucket=self.active.setdefault(ctx.request_id,set());bucket.update(name for name in names if name in registry)
        return set(bucket)
    def active_skills(self,ctx):return set(self.active.get(ctx.request_id,()))

    def catalogue(self):return self.knowledge.community_catalog(self.business.community_catalog(),include_demo=not self.business.has_real_properties())
    def clear_request(self,request_id):
        for key in [key for key in self.call_cache if isinstance(key,tuple) and len(key)>3 and key[3]==request_id]:self.call_cache.pop(key,None)
    def log(self,ctx,tool,event,**fields):
        row={'at':time.time(),'request_id':ctx.request_id,'customer_id':ctx.customer_id,'broker_id':ctx.broker_id,'tool':tool,'event':event,**fields}
        with self.log_path.open('a',encoding='utf-8') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    async def sync(self,fn,*args,**kwargs):
        await self.slots.acquire()
        async def work():
            try:return await asyncio.to_thread(fn,*args,**kwargs)
            finally:self.slots.release()
        task=asyncio.create_task(work());self.workers.add(task);task.add_done_callback(self.workers.discard)
        task.add_done_callback(lambda done:done.exception() if not done.cancelled() else None)
        return await asyncio.shield(task)
    @staticmethod
    def transient(e):
        return isinstance(e,(ConnectionError,TimeoutError)) or isinstance(e,sqlite3.OperationalError) and (getattr(e,'sqlite_errorcode',None) in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED} or 'locked' in str(e).lower())
    def tighten(self,args,ctx):
        data=args.model_dump(exclude_none=True);hard=self.constraints.get(ctx.request_id,{})
        for field in ('max_price_yuan','max_floor','max_metro_distance_m','max_area_sqm'):
            if field in hard:data[field]=min(data.get(field,hard[field]),hard[field])
        if 'min_floor' in hard:data['min_floor']=max(data.get('min_floor',hard['min_floor']),hard['min_floor'])
        if 'min_area_sqm' in hard:data['min_area_sqm']=max(data.get('min_area_sqm',hard['min_area_sqm']),hard['min_area_sqm'])
        for field in ('bedrooms','region','require_elevator','orientation','decoration'):
            if field in hard:data[field]=hard[field]
        if ctx.request_id in self.community_scopes:
            allowed=self.community_scopes[ctx.request_id]
            data['community_keys']=sorted(allowed if data.get('community_keys') is None else allowed.intersection(data['community_keys']))
        return data
    async def execute(self,name,arguments,ctx):
        started=time.monotonic();self.log(ctx,name,'start');attempts=0;cache_key=None
        budget=self.timeouts.get(name,self.timeout)
        try:
            self.business.session(ctx.broker_id,ctx.customer_id,ctx.session_id)
            args=self.schemas[name].model_validate(arguments)
            if ctx.request_id in self.inspections and name in ('search_listings','draft_recommendation'):raise ValueError('当前为指定房源查阅，不会切换到其他房源或提交推荐。')
            if name in self.READ_ONLY and not (name=='query_customer_memory' and getattr(args,'refresh',False)):
                cache_key=json.dumps(args.model_dump(exclude_none=True),sort_keys=True,ensure_ascii=False,default=str)
                cached=self.call_cache.get((ctx.broker_id,ctx.customer_id,ctx.session_id,ctx.request_id,name,cache_key))
                if cached is not None:
                    result=copy.deepcopy(cached);result['reused']=True;result['attempts']=0
                    if name=='search_listings':self.candidates[ctx.request_id]=copy.deepcopy(result['data'])
                    self.log(ctx,name,'reused')
                    self.traces.setdefault(ctx.request_id,[]).append({'tool':name,'status':'reused',
                        'args':{key:arguments[key] for key in ('max_price_yuan','bedrooms','min_floor','max_floor','community_keys','property_ids','refresh') if isinstance(arguments,dict) and key in arguments}})
                    return result
            if name=='think':
                data=args.model_dump();self.thoughts.setdefault(ctx.request_id,[]).append(data)
            elif name=='load_skill':
                skill=registry.get(args.name);self.activate(ctx,args.name)
                data={'name':skill.name,'tools':list(skill.tools),'instructions':skill.instructions}
            elif name=='draft_recommendation':
                allowed={r['property_id'] for r in self.candidates.get(ctx.request_id,[]) if not mismatches(r,self.constraints.get(ctx.request_id,{})) and (ctx.request_id not in self.community_scopes or r['community_key'] in self.community_scopes[ctx.request_id])}
                if len(set(args.property_ids))!=len(args.property_ids) or not set(args.property_ids).issubset(allowed):raise ValueError('报告只能选择当前候选房源，不能引用其他房源。')
                data=args.model_dump()
            else:
                async with asyncio.timeout(budget):
                    for attempt in range(2):
                        attempts=attempt+1
                        try:
                            if name=='search_listings':
                                effective=self.tighten(args,ctx);data=await self.sync(self.business.search,effective)
                                self.candidates[ctx.request_id]=data
                            elif name=='search_communities':
                                catalog=await self.sync(self.catalogue);hard=self.constraints.get(ctx.request_id,{})
                                data=await self.sync(self.knowledge.discover,args.query,catalog,hard.get('region') or args.region,args.limit,self.exact_terms.get(ctx.request_id))
                                locked=self.community_scopes.get(ctx.request_id)
                                if locked is not None:data=[row for row in data if row['community_key'] in locked]
                                self.communities[ctx.request_id]={row['community_key']:row for row in data}
                            elif name=='get_property_details':
                                record=await self.sync(self.business.property_details,args.property_id);data=record
                                if record:
                                    self.lookups.setdefault(ctx.request_id,{})[record['property_id']]=record
                                    if ctx.request_id in self.inspections:self.candidates[ctx.request_id]=[record]
                            elif name=='query_customer_memory':
                                if args.phase=='light':
                                    async def load():return await self.sync(self.memory.recall_light,ctx.broker_id,ctx.customer_id)
                                    data=await self.memory_contexts.get(ctx,load,refresh=args.refresh)
                                else:
                                    async def load():return await self.sync(self.memory.recall,ctx.broker_id,ctx.customer_id,args.query)
                                    data=await self.memory_contexts.get(ctx,load,refresh=args.refresh)
                                    # A light (core-only) snapshot cannot answer an explicit
                                    # memory query, so upgrade it once and then coalesce.
                                    if isinstance(data,dict) and data.get('retrieval_status')=='light':
                                        data=await self.memory_contexts.get(ctx,load,refresh=True)
                            else:
                                rows=self.candidates.get(ctx.request_id,[]);regions={r['community_key']:(r['region'],r['is_demo']) for r in rows}
                                regions.update({r['community_key']:(r['region'],r['is_demo']) for r in self.lookups.get(ctx.request_id,{}).values()})
                                regions.update({k:(r['region'],r['is_demo']) for k,r in self.communities.get(ctx.request_id,{}).items()})
                                if not set(args.community_keys).issubset(regions):raise ValueError('资料检索必须关联当前候选房源的小区。')
                                groups=await asyncio.gather(*(self.sync(self.knowledge.search,key,regions[key][0],args.query,regions[key][1]) for key in args.community_keys))
                                data=[];seen=set()
                                for group in groups:
                                    for doc in group:
                                        if doc['chunk_id'] not in seen:data.append(doc);seen.add(doc['chunk_id'])
                            break
                        except Exception as e:
                            if attempt or not self.transient(e):raise
                            self.log(ctx,name,'retry',attempt=attempts);await asyncio.sleep(.08)
            def public_data(value):
                if isinstance(value,dict):return {k:public_data(v) for k,v in value.items() if k!='source'}
                if isinstance(value,list):return [public_data(v) for v in value]
                return value
            result={'tool':name,'status':'ok' if data else 'empty','data':public_data(data),'attempts':max(1,attempts)}
        except (ValidationError,ValueError,KeyError) as e:result={'tool':name,'status':'error','error_code':'invalid_arguments','message':str(e)[:400],'data':None}
        except PermissionError as e:result={'tool':name,'status':'error','error_code':'forbidden','message':str(e),'data':None}
        except TimeoutError:result={'tool':name,'status':'error','error_code':'timeout','message':'该项读取超时，可继续查看其他可用信息。','data':None}
        except Exception as e:result={'tool':name,'status':'error','error_code':type(e).__name__,'message':'该项读取暂不可用。','data':None}
        if cache_key is not None and name in self.READ_ONLY and result['status'] in ('ok','empty'):
            self.call_cache[(ctx.broker_id,ctx.customer_id,ctx.session_id,ctx.request_id,name,cache_key)]=copy.deepcopy(result)
        self.log(ctx,name,'finish',status=result['status'],elapsed_ms=round((time.monotonic()-started)*1000,1))
        # Trajectory for plan-execution scoring; argument bodies are reduced to
        # the fields a golden path actually asserts.
        step={'tool':name,'status':result['status'],'error_code':result.get('error_code'),'attempts':result.get('attempts',1),
            'args':{key:arguments[key] for key in ('max_price_yuan','bedrooms','min_floor','max_floor','community_keys','property_ids','refresh') if isinstance(arguments,dict) and key in arguments}}
        if name=='think':step['thought']=(arguments.get('thought') or '')[:200]
        self.traces.setdefault(ctx.request_id,[]).append(step)
        return result
    def definitions(self,ctx=None):
        descriptions={'think':'分析或补查前先写一句判断和下一步；think只记录计划，不代替检索。','search_listings':'按客户硬条件及community_keys/property_ids筛选房源，空列表代表无匹配；预算单位元，不会放宽宿主硬条件。','search_communities':'先按学校、医疗、商超、公园等需求全库检索小区资料，返回小区标识及证据；覆盖话题不等于满足距离或入学资格。','get_property_details':'按准确房源编号查阅房源，并定位所属小区；查阅不等于符合客户硬条件。','retrieve_area_info':'检索已发现小区或当前候选房源关联的小区与区域资料。','query_customer_memory':'读取当前客户的会话记忆上下文，默认复用首次召回缓存；仅当用户要求重新读取或确需额外历史细节时使用refresh=true。不能切换客户。','load_skill':'按需加载本地业务Skill说明；加载后才会看到该Skill的工具Schema。','draft_recommendation':'选择候选房源并生成分析草稿，只准备预览，不直接保存。'}
        # Progressive disclosure, level 3: only activated skills expose tools.
        visible=None if ctx is None else registry.tools_for(self.active_skills(ctx))
        return [{'type':'function','function':{'name':name,'description':descriptions[name],'parameters':schema.model_json_schema()}} for name,schema in self.schemas.items() if visible is None or name in visible]
    async def drain(self):
        while self.workers:await asyncio.gather(*tuple(self.workers),return_exceptions=True)
