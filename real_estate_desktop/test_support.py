"""Internal test doubles for unit checks and --self-check; no application fallback."""
import re
from .catalog import NUMBER,ListingFacts,MaterialFacts,Proposal
from .domain import chinese_number

def fixture_proposal(messages):
    """Deterministic extraction for isolated tests only; never selected by the application."""
    raw='\n'.join(m['content'] for m in messages if m['role']=='user');values={};evidence={}
    def match(key,pattern,convert=lambda m:m[1]):
        matches=list(re.finditer(pattern,raw))
        if matches:
            m=matches[-1];values[key]=convert(m);evidence[key]=m.group()
    match('community_name',r'小区(?:名称)?[：:\s]+([^，。；\n]{2,40})')
    if 'community_name' not in values:match('community_name',r'(?:添加|新增|录入)(?:一套)?房源[：:\s]+([^，。；\n]{2,30})')
    match('region',r'(?:区域|地区)[：:\s]+([^，。；\n]{2,40})')
    match('price_yuan',r'(?:总价|售价|价格)[：:\s]*(?:改为|改成|调整为)?\s*('+NUMBER+r')\s*(万元|万|元)',lambda m:int(chinese_number(m[1])*(10000 if m[2] in ('万','万元') else 1)))
    match('area_sqm','('+NUMBER+r')\s*(?:㎡|平米|平方米|平)',lambda m:float(chinese_number(m[1])))
    match('bedrooms','('+NUMBER+r')\s*(?:室|居)',lambda m:int(chinese_number(m[1])))
    match('floor',r'(?:楼层[：:\s]*|第)(\d+)\s*(?:层|楼|/)',lambda m:int(m[1]))
    match('total_floors',r'(?:共|总高|总楼层[：:\s]*)(\d+)\s*(?:层|楼)?',lambda m:int(m[1]))
    matches=list(re.finditer(r'(?:楼层[：:\s]*)?(\d+)\s*/\s*(\d+)\s*(?:层|楼)',raw))
    if matches:
        m=matches[-1];values.update(floor=int(m[1]),total_floors=int(m[2]));evidence.update(floor=m.group(),total_floors=m.group())
    matches=list(re.finditer(r'无电梯|没有电梯|不带电梯|没电梯|有电梯|带电梯|电梯房|电梯[：:\s]*(?:没有|有|无|是|否)',raw))
    if matches:m=matches[-1];values['has_elevator']=not bool(re.search(r'无|没有|不带|没电梯|否',m.group()));evidence['has_elevator']=m.group()
    match('orientation',r'朝向[：:\s]*([东西南北]{1,2})')
    match('decoration',r'(精装|简装|毛坯|豪装)')
    match('building_age',r'房龄[：:\s]*('+NUMBER+r')年',lambda m:int(chinese_number(m[1])))
    match('metro_distance_m',r'(?:地铁|距地铁)[^，。；\n]{0,6}?('+NUMBER+r')\s*米',lambda m:int(chinese_number(m[1])))
    material=bool(re.search(r'小区资料|小区介绍|社区资料|区域资料|区域介绍',raw));listing=bool(re.search(r'房源|总价|售价',raw))
    target='both' if material and listing else 'material' if material else 'property' if listing else 'clarify'
    mat=None
    if material:
        texts=list(re.finditer(r'(?:资料正文|小区介绍|区域介绍)[：:]\s*([^\n]+)',raw));body=texts[-1][1] if texts else raw
        dates=list(re.finditer(r'资料日期[：:\s]*(\d{4}-\d{2}-\d{2})',raw));sources=list(re.finditer(r'(?:资料来源|来源)[：:\s]*([^，。；\n]+)',raw))
        mat=MaterialFacts(community_name=values.get('community_name'),region=values.get('region'),scope='region' if re.search(r'区域资料|区域介绍',raw) else 'community',text=body,as_of=dates[-1][1] if dates else None)
    return Proposal(target=target,property=ListingFacts(**values,evidence=evidence) if listing else None,material=mat)


class ScriptedModel:
    def memory_facts(self,text):
        from .search_planning import customer_preferences
        return [{'memory_type':'episodic','attribute':key,'value':__import__('json').dumps(value,ensure_ascii=False),'content':f'客户本次购房偏好 {key}：{value}','evidence':text[:1000]} for key,value in customer_preferences(text).items()][:8]
    async def memory_json(self,prompt,data,schema):
        """Deterministic component protocol fixtures, injected only by engineering checks."""
        if 'trusted_context' in data:
            ctx=data['trusted_context'];source=data['target_user_message'];records=[]
            for index,fact in enumerate(self.memory_facts(source['content'])):
                semantic=fact['memory_type']=='semantic'
                records.append({'id':ctx['allocated_memory_ids'][index],'memory_type':fact['memory_type'],'subject':'customer','attribute':fact['attribute'],'value':fact['value'],'content':fact['content'],
                    'scope':{**ctx['scope'],'level':'user' if semantic else 'project','project_id':None if semantic else ctx['scope']['project_id']},
                    'source':{'conversation_id':ctx['conversation_id'],'message_id':source['id'],'speaker':'user','evidence':fact['evidence']},
                    'time':{'event_time':None,'recorded_at':ctx['recorded_at'],'valid_from':None,'valid_to':None},'status':'active','supersedes':None,'retention':{'pinned':False,'evidence':None}})
            return {'long_term_memory_candidates':records,'retention_updates':[]}
        decisions=[]
        for index,candidate in enumerate(data['candidates']):
            old=next((r for r in data['old_memories'] if r['attribute']==candidate['attribute'] and r['scope']==candidate['scope']),None)
            decisions.append({'candidate_index':index,'action':'NOOP' if old and old['value']==candidate['value'] else 'UPDATE' if old else 'ADD','target_memory_id':old['id'] if old else None,'reason':'工程检查的确定性比较结果'})
        return {'decisions':decisions,'retention_decisions':[]}
    async def extract_json(self,prompt,data,schema):
        """Deterministic component fixtures; unit tests may override this method."""
        if schema.get('title')=='DraftRevision':
            return {'property_ids':[item['property_id'] for item in data.get('current_draft',{}).get('properties',[])],
                    'note':'工程测试替身保留原选择。'}
        timeline=[f"{row['key']}：{row['value']}" for row in data.get('preference_versions',[])][-4:]
        return {'demand_timeline':timeline,'viewed':[],'open_items':[],
                'summary':'工程测试替身生成的跟进草稿：本次会话的沟通要点已记录。'}
    async def plan_search(self,context):
        plan=context['_host_plan']
        return {'route':plan['route'],'query':plan['query'],'questions':plan['questions'],'exact_terms':plan['exact_terms']}
    async def generate(self,messages,tools,**kwargs):
        if any(t['function']['name']=='propose_catalog_entry' for t in tools):
            proposal=fixture_proposal(messages)
            return {'role':'assistant','content':'','tool_calls':[{'id':'fixture-proposal','type':'function','function':{'name':'propose_catalog_entry','arguments':proposal.model_dump_json()}}]}
        return {'role':'assistant','content':'工程测试替身输出：请结合候选资料核对房源条件。'}
    async def aclose(self):pass
