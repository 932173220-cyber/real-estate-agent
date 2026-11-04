"""Model-extracted follow-up notes with host-side grounding checks.

The model may summarise, but the host owns every claim that reaches the draft:
property IDs must be this session's candidates, and a feedback quote must exist
verbatim in this session's messages. Anything else is dropped before rendering.
"""
from __future__ import annotations
import json,re
from pydantic import ValidationError
from .domain import FollowUpDraft,FollowUpViewed,PROPERTY_ID

FOLLOWUP_PROMPT='''你是房产经纪的跟进记录整理助手。只输出符合所给JSON Schema的对象，不回答客户、不调用工具、不写Markdown。
输入是当前客户这一次会话的资料：
- conversation：按时间排列的经纪人与助手消息；经纪人的话可能是在转述客户原话。
- preference_versions：该客户已有结构化偏好的版本记录，按时间从早到晚，active 表示当前生效。
- session_records：本会话已经确认保存的推荐记录。
- session_candidates：本会话出现过的房源编号，只能从这里选择房源。
规则：
1. 只能使用这些资料中有依据的内容。不得编造客户看过、联系过、同意过、成交或任何未发生的行为；不得把助手建议当成客户事实。
2. viewed 的 property_id 必须来自 session_candidates。feedback 必须是 conversation 中某条消息的连续原文片段，逐字复制、可截短，不得改写或补充评价；找不到原文依据就不要写反馈。不确定是否沟通过的房源不要列入。
3. demand_timeline 按时间顺序概括客户明确提出的购房需求与变化（预算、户型、楼层、面积、配套等），每条一句话，保留限定条件，不推断未表达的需求。
4. open_items 只列会话中明确提出但尚未解决的事项（待核验、待补充、待回复）；没有就返回空数组。
5. summary 用两三句话概括本次沟通和当前状态，不承诺、不夸大。
6. 不写“已带看”“已成交”“客户很满意”等没有原文依据的结论，不输出客户身份判断。'''

def _clean(value,limit):
    text=re.sub(r'\s+',' ',(value or '').strip()).strip('“”"\'‘’')
    return text[:limit]

def _normalise(text):return re.sub(r'[\s“”"\'‘’]','',text or '')

def grounded(feedback,conversation):
    """True only when the quote is a contiguous fragment of a saved message."""
    quote=_normalise(feedback)
    if len(quote)<2:return False
    return quote in _normalise('\n'.join(message['content'] for message in conversation))

def _conversation(rows,*,limit=40,total=12000):
    """Newest messages first for the cap, then restored to chronological order."""
    selected=[];used=0
    for row in reversed(rows[-limit:]):
        role='经纪人' if row['role']=='user' else '助手' if row['role']=='assistant' else row['role']
        content=_clean(row.get('content'),700 if role=='经纪人' else 500)
        if not content:continue
        if selected and used+len(content)>total:break
        selected.append({'role':role,'content':content});used+=len(content)
    return list(reversed(selected))

def _versions(memory,broker,cid,limit=30):
    rows=memory.versions(broker,cid)[:limit]
    values=[]
    for row in reversed(rows):
        try:value=json.loads(row['value'])
        except (TypeError,ValueError):value=row['value']
        values.append({'key':row['key'],'value':value,'version':row['version'],'active':bool(row['active']),
            'source':_clean(row['source'],160)})
    return values

def _recommendations(business,broker,cid,sid,limit=10):
    """Recent confirmed recommendations of this session, newest first, plus their full rows."""
    records=[];details={}
    for record in business.records(broker,cid):
        if record['session_id']!=sid:continue
        try:payload=json.loads(record['payload'])
        except ValueError:continue
        properties=payload.get('properties') or []
        for item in properties:
            if item.get('property_id'):details[item['property_id']]=item
        records.append({'kind':payload.get('kind'),'created':record['created'],
            'source_text':_clean(payload.get('source_text'),200),
            'properties':[{'property_id':p.get('property_id'),'community_name':p.get('community_name'),'price_yuan':p.get('price_yuan')}
                for p in properties if p.get('property_id')]})
        if len(records)>=limit:break
    return records,details

def build_context(business,memory,broker,cid,sid):
    """Read-only snapshot of this session; synchronous so it can run in a worker thread."""
    history=business.history(broker,cid,sid)
    records,details=_recommendations(business,broker,cid,sid)
    for draft in business.pending(broker,cid,sid):
        try:payload=json.loads(draft['payload'])
        except ValueError:continue
        for item in payload.get('properties') or []:
            if item.get('property_id'):details[item['property_id']]=item
    conversation=_conversation(history)
    # Candidate IDs come from the full session, not only the truncated prompt window.
    for message in history:
        for property_id in PROPERTY_ID.findall(message.get('content') or ''):
            if property_id in details:continue
            record=business.property_details(property_id)
            if record:details[record['property_id']]=record
    return {'conversation':conversation,'preference_versions':_versions(memory,broker,cid),
        'session_records':records,'session_candidates':sorted(details),'candidate_details':details}

def sanitise(draft,details,conversation):
    """Drop unsupported claims and return host-visible notes for what was removed."""
    timeline=[]
    for item in draft.demand_timeline:
        cleaned=_clean(item,160)
        if cleaned and cleaned not in timeline:timeline.append(cleaned)
    viewed=[];seen=set();unknown=[];ungrounded=[]
    for item in draft.viewed:
        property_id=_clean(item.property_id,80)
        if property_id not in details:
            unknown.append(property_id);continue
        if property_id in seen:continue
        seen.add(property_id)
        feedback=_clean(item.feedback,200)
        if feedback and not grounded(feedback,conversation):
            ungrounded.append(property_id);feedback=''
        viewed.append(FollowUpViewed(property_id=property_id,feedback=feedback))
    open_items=[]
    for item in draft.open_items:
        cleaned=_clean(item,160)
        if cleaned and cleaned not in open_items:open_items.append(cleaned)
    summary=_clean(draft.summary,1200)
    if not summary:raise ValueError('模型返回的跟进记录摘要为空，请重新生成。')
    notes=[]
    if unknown:notes.append('已省略会话候选之外的房源：'+'、'.join(dict.fromkeys(unknown))+'。')
    if ungrounded:notes.append('已省略无法在会话原文中核对的反馈：'+'、'.join(dict.fromkeys(ungrounded))+'。')
    return FollowUpDraft(demand_timeline=timeline,viewed=viewed,open_items=open_items,summary=summary),notes

def render(draft,details,notes):
    lines=['# 跟进记录草稿','']
    if draft.demand_timeline:lines+=['## 需求脉络',*[f'- {item}' for item in draft.demand_timeline],'']
    if draft.viewed:
        lines+=['## 已沟通房源']
        for item in draft.viewed:
            name=(details.get(item.property_id) or {}).get('community_name') or ''
            head=f"- {item.property_id}"+(f" · {name}" if name else '')
            lines.append(head+(f"｜客户原话：{item.feedback}" if item.feedback else '｜（本轮未记录客户反馈）'))
        lines.append('')
    if draft.open_items:lines+=['## 待办事项',*[f'- {item}' for item in draft.open_items],'']
    lines+=['## 摘要',draft.summary,'']
    if notes:lines+=['> '+' '.join(notes),'']
    return '\n'.join(lines).strip()

async def draft_followup(runtime,ctx):
    data=await runtime.tools.sync(build_context,runtime.business,runtime.memory,ctx.broker_id,ctx.customer_id,ctx.session_id)
    payload={key:value for key,value in data.items() if key!='candidate_details'}
    raw=await runtime.extract(FOLLOWUP_PROMPT,payload,FollowUpDraft.model_json_schema())
    try:draft=FollowUpDraft.model_validate(raw)
    except ValidationError as error:raise ValueError('模型返回的跟进记录不符合格式，请重新生成。') from error
    clean,notes=sanitise(draft,data['candidate_details'],data['conversation'])
    return render(clean,data['candidate_details'],notes)
