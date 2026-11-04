"""Revision of a pending recommendation draft.

The model only picks which session candidates stay in the draft. The host then
rebuilds the report from database facts, so a revision can never invent prices,
areas or an extra property that the broker did not name.
"""
from __future__ import annotations
import re
from pydantic import ValidationError
from .domain import DraftRevision

REVISION_PROMPT='''你是房源推荐草稿的修订助手。只输出符合所给JSON Schema的对象，不写解释性长文、不虚构房源。
输入：
- instruction：经纪人这一次的修改意见。
- current_draft：当前待确认草稿中的房源（数据库事实）。
- session_candidates：本会话出现过的全部房源编号及简要信息。
规则：
1. property_ids 是修改后的最终房源顺序，只能来自 session_candidates，不能编造编号或房源。
2. 不在 current_draft 中的房源，只有 instruction 明确写出该房源编号时才可加入；否则只能删除、保留或调整当前草稿内的房源。
3. “去掉/不要/删除/换掉”某房源就去掉；“只看/保留/只要”就只留指定房源；“再便宜点/贵了”优先保留总价更低的房源；“加上”只在 instruction 点名编号时加入。
4. 无法按意见调整时保留原选择，并在 note 用一句话说明原因。不要写客户已看房、已同意等没有依据的行为。
5. note 只说明这次修改了什么，不超过 80 字；不要输出报告正文。'''

def compact(row):
    keys=('property_id','community_name','region','price_yuan','area_sqm','bedrooms','floor','total_floors','orientation','decoration','has_elevator','metro_distance_m')
    return {key:row.get(key) for key in keys}

def looks_like_revision(text):
    """A revision needs a draft-specific edit verb, not a new hard condition."""
    text=text or ''
    if re.search(r'(?:改一下|改下|修改一下|调整一下|重做|重新生成|换个|换一|再便宜|便宜点|便宜些|贵了|价格再|少一点|多一点)',text):return True
    if re.search(r'(?:把|将).{0,14}(?:改成|改为|换成|去掉|删掉|删除|加上|加入|保留|只要)',text):return True
    if re.search(r'(?:去掉|删掉|删除|移除|换掉|换成|不要|保留|只要|只看|加上|加入|添加)',text) and re.search(r'(?:P\d{3,}|[A-Z]{1,3}\d?-\d{2}|这套|那套|房源|推荐|报告|草稿|这几套|第一套|第二套)',text):return True
    return False

async def revise_selection(runtime,ctx,payload,details,instruction):
    current=[item for item in payload.get('properties') or [] if item.get('property_id') in details]
    current_ids={item['property_id'] for item in current}
    candidates=[{'property_id':property_id,'community_name':details[property_id].get('community_name'),
        'price_yuan':details[property_id].get('price_yuan'),'area_sqm':details[property_id].get('area_sqm'),
        'bedrooms':details[property_id].get('bedrooms')} for property_id in sorted(details)]
    data={'instruction':instruction,
        'current_draft':{'kind':payload.get('kind'),'properties':[compact(item) for item in current]},
        'session_candidates':candidates}
    raw=await runtime.extract(REVISION_PROMPT,data,DraftRevision.model_json_schema())
    try:revision=DraftRevision.model_validate(raw)
    except ValidationError as error:raise ValueError('模型返回的草稿修改不符合格式，请重新说明修改意见。') from error
    ordered=[]
    for property_id in revision.property_ids:
        property_id=property_id.strip()
        if property_id in ordered or property_id not in details:continue
        # A candidate outside the draft may be added only when the broker named it.
        if property_id not in current_ids and property_id not in instruction:continue
        ordered.append(property_id)
    return ordered,revision.note
