from __future__ import annotations
import asyncio,json,sys,tempfile
from pathlib import Path
from datetime import datetime

async def self_check(destination):
    from .config import Settings
    from .runtime import Runtime
    from .test_support import ScriptedModel
    from .skill_registry import CATALOGUE,load_skill
    text_memory='只记住客户：客户习惯居家办公，这次希望有独立书房。'
    class CheckModel(ScriptedModel):
        def memory_facts(self,text):
            if text==text_memory:return [dict(memory_type='semantic',attribute='work_pattern',value='居家办公',content='客户习惯居家办公',evidence='客户习惯居家办公'),dict(memory_type='episodic',attribute='purchase_workspace',value='独立书房',content='这次希望有独立书房',evidence='这次希望有独立书房')]
            return super().memory_facts(text)
    report={'at':datetime.now().isoformat(),'frozen':bool(getattr(sys,'frozen',False)),'model_backend':'internal test double (no API request)','success':False}
    try:
        with tempfile.TemporaryDirectory(prefix='property-release-check-') as folder:
            settings=Settings(Path(folder));r=Runtime(settings,model=CheckModel())
            try:
                c=r.create_customer('打包自检客户');cid=c['customer_id'];sid=r.new_session(cid)
                result=await r.chat('预算400万以内，三室，不要一楼，最好离地铁近',cid,sid)
                draft_id=result['draft_id'];report['draft_generated']=bool(draft_id);report['recommendation_before_confirmation']=len(r.records(cid))
                report['skill_files']=sum(bool(load_skill(name)) for name in CATALOGUE)
                report['demo_properties']=len(r.business.properties());report['demo_documents']=len(r.knowledge.documents())
            finally:await r.close()
            r=Runtime(settings,model=CheckModel())
            try:
                result=await r.confirm(cid,sid,draft_id,True);repeat=await r.confirm(cid,sid,draft_id,True)
                report.update(checkpoint_restored=result['status']=='saved',idempotent=repeat['replayed'],recommendations=len(r.records(cid)),memory_budget=r.profile(cid).get('max_price_yuan'))
                report['success']=report['draft_generated'] and report['recommendation_before_confirmation']==0 and report['skill_files']==5 and report['checkpoint_restored'] and report['idempotent'] and report['recommendations']==1 and report['memory_budget']==4000000
                sid2=r.new_session(cid)
                community=await r.chat('希望有小学和医院，买菜方便，不保存',cid,sid2)
                parallel=await r.chat('预算400万，三室，不要一楼，小学医院配套，不保存',cid,sid2)
                details=await r.chat('P013这套房子的小区配套如何？',cid,sid2)
                report['bidirectional_routes']=community.get('search_route')=='community_first' and parallel.get('search_route')=='parallel' and details.get('search_route')=='property_details' and '澄湾雅苑' in details['report']
                report['memory_nodes']='recall_memory' in r.workflow.graph.nodes and 'update_memory' in r.workflow.graph.nodes
                report['success']=report['success'] and report['bidirectional_routes'] and report['memory_nodes']
                typed=await r.chat(text_memory,cid,sid2);await r.confirm(cid,sid2,typed['draft_id'],True)
                memories=r.memory_records(cid);report['typed_memory_layers']=bool(memories['semantic']) and bool(memories['episodic'])
                report['success']=report['success'] and report['typed_memory_layers']
                entry=r.catalog.create()
                text='添加房源：春江花园，小区：春江花园，区域：滨江区，总价350万，105平米，三室，楼层8/18层，有电梯。\n添加小区资料，小区：春江花园，区域：滨江区。小区介绍：东门有公交站。'
                draft=await r.catalog.chat(entry['entry_id'],text,'online');report['catalog_preview']=draft['status']=='pending'
                await r.close();r=Runtime(settings,model=CheckModel())
                out=await r.catalog.confirm(draft['entry_id'],draft['revision'],True);repeat=await r.catalog.confirm(draft['entry_id'],draft['revision'],True)
                property=r.business.property(out['property_id']);docs=r.knowledge.search(property['community_key'],'滨江区','公交站',False)
                report.update(catalog_restart_confirm=out['status']=='saved',catalog_idempotent=repeat['replayed'],catalog_material_retrievable=bool(docs),catalog_does_not_change_customer_memory=r.profile(cid)['max_price_yuan']==4000000)
                report['success']=report['success'] and report['catalog_preview'] and all(report[k] for k in ('catalog_restart_confirm','catalog_idempotent','catalog_material_retrievable','catalog_does_not_change_customer_memory'))
            finally:await r.close()
    except Exception as error:report['error_type']=type(error).__name__;report['detail']=str(error)[:500]
    path=Path(destination).resolve();path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['success'] else 1

def launch():
    if len(sys.argv)==3 and sys.argv[1]=='--self-check':raise SystemExit(asyncio.run(self_check(sys.argv[2])))
    from .app import main
    main()
