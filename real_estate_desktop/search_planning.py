"""Routing and evidence terms, independent of database and workflow implementation."""
from __future__ import annotations
import re,unicodedata
from .domain import parse_request,Schema
from typing import Literal
from pydantic import Field

class ConstraintProposal(Schema):
    """A hard condition read out of the message, with the quote it came from."""
    field:Literal['max_price_yuan','bedrooms','min_floor','max_floor','min_area_sqm','max_area_sqm','orientation','decoration','region','require_elevator','max_metro_distance_m']
    value:str=Field(min_length=1,max_length=40)
    quote:str=Field(min_length=1,max_length=200)
    inferred:bool=False

class RouteDecision(Schema):
    route:Literal['community_first','listing_first','parallel','property_details','community_listings']
    query:str=Field(min_length=1,max_length=1200)
    questions:list[str]=Field(default_factory=list,max_length=3)
    exact_terms:list[str]=Field(default_factory=list,max_length=8)
    preference_topics:list[str]|None=Field(default=None,max_length=10)
    preference_quote:str=Field(default='',max_length=500)
    constraints:list[ConstraintProposal]=Field(default_factory=list,max_length=6)

AMENITIES={
 '小学':('小学',),'中学':('初中','中学','高中'),'幼儿园':('幼儿园',),
 '教育':('教育','学校','小学','中学','幼儿园'),
 '医疗':('医疗','医院','门诊','卫生服务','诊所'),
 '购物':('商超','超市','商场','购物','菜市场','生鲜','采买','买菜'),
 '公园':('公园','绿地','绿道','步道','健身','散步','骑行'),
 '安静':('安静','噪声','隔音','居住环境'),
 '交通':('地铁','公交','交通','通勤'),
}
TRIGGERS={**AMENITIES,'教育':('学校','教育','学区','上学','孩子读书'),'医疗':('医院','看病','就医','医疗','卫生服务','诊所'),'购物':('超市','商超','商场','购物','买菜','采买','生鲜'),'公园':('公园','绿地','遛狗','散步','跑步','骑行'),'安静':('安静','不吵','噪音','噪声','隔音')}

SOFT_AMENITY=('最好','尽量','希望','优先','偏好','倾向','如果可以','若能','能的话','比较喜欢','更希望')
HARD_AMENITY=('必须','一定要','务必','要求','不能没有','非要','只要','硬性','必须有','一定要有')

def normal(value):return re.sub(r'\s+','',unicodedata.normalize('NFKC',value)).casefold()

def amenity_hardness(query):
    """Per-group verdict: soft only when every mention is softened and none is bare/hard."""
    verdicts={}
    for name,triggers in TRIGGERS.items():
        found=False;soft_only=True
        for trigger in triggers:
            for match in re.finditer(re.escape(trigger),query):
                found=True
                prefix=query[max(0,match.start()-12):match.start()]
                soft=max((prefix.rfind(word) for word in SOFT_AMENITY),default=-1)
                hard=max((prefix.rfind(word) for word in HARD_AMENITY),default=-1)
                if hard>=0 and hard>=soft:soft_only=False
                elif soft<0:soft_only=False  # a bare amenity statement is a requirement
        if found:verdicts[name]='soft' if soft_only else 'hard'
    return verdicts

def preserve_entity_names(host_terms,model_terms):
    """Keep source-derived full names when a model omits or shortens them."""
    output=list(host_terms)
    generic={'小学','中学','幼儿园','医院','公园','超市','购物中心'}
    for term in model_terms:
        longer=[value for value in host_terms if term not in generic and len(term)>=4 and value!=term and value.endswith(term)]
        output.extend(longer or [term])
    return list(dict.fromkeys(output))

def customer_preferences(text):
    values=parse_request(text)['explicit_preferences']
    if re.search(r'(?<![A-Za-z0-9_-])(?:P\d{3,}|R-[A-Za-z0-9_-]{4,})',text,re.I):
        # Describing a property's price/rooms is not a change to the customer's preferences.
        if not re.search(r'预算|我最多|我最高|我能接受',text):values.pop('max_price_yuan',None)
        if not re.search(r'(?:希望|需要|想要|要求|我想|我要).{0,6}(?:室|居)',text):values.pop('bedrooms',None)
    return values

def amenity_terms(query):
    groups=[]
    for name,triggers in TRIGGERS.items():
        if any(t in query for t in triggers):groups.append(name)
    if any(n in groups for n in ('小学','中学','幼儿园')) and '教育' in groups:groups.remove('教育')
    exact=[]
    for m in re.finditer(r'[\u4e00-\u9fffA-Za-z0-9·]{0,28}?(?:小学|中学|幼儿园|医院|公园|购物中心|超市)',query):
        name=m.group()
        prefixes=('我想住在','我想住','我想找','我想','我希望','请帮我找','请帮我','帮我找','先看看','先看','看看','看一下','先找','先查','只看','同时希望有','同时要有','同时有','也要有','还要有','希望靠近','希望有','想找','想住','想要','最好离','最好有','最好','必须靠近','必须有','必须','一定要','周边要有','附近要有','附近有','周围有','周边有','旁边有','附近','要有','靠近','住在','位于','推荐','要求','既要','一所','一个','一家','离','找','有')
        for _ in range(5):
            prefix=next((p for p in prefixes if name.startswith(p)),None)
            if not prefix:break
            name=name[len(prefix):]
        name=name.strip()
        if name and name not in {'小学','中学','幼儿园','医院','公园','超市','购物中心','好学校','大医院','大型医院','三甲医院','大型超市','好小学','重点小学','重点中学'} and len(name)>next((len(s)+1 for s in ('小学','中学','幼儿园','医院','公园','购物中心','超市') if name.endswith(s)),100):exact.append(name)
    if '三甲' in query:exact.append('三甲')
    return groups,list(dict.fromkeys(exact))

def choose_plan(text,history,preferences,catalog,properties):
    users=[r['content'] for r in history if r['role']=='user']
    if not users or users[-1]!=text:users.append(text)
    start=0
    for i,line in enumerate(users):
        if re.search(r'重新找|重新推荐|换个需求|不考虑之前|配套不限|不限配套',line):start=i
    context='\n'.join(users[start:][-10:])[-5000:]
    groups,exact=amenity_terms(context)
    # Explicitly withdrawn soft preferences do not carry into follow-up searches.
    for key,pattern in [('教育',r'不考虑(?:学校|学区|教育)|不用(?:学校|学区)|教育不限'),('医疗',r'不考虑医院|医院不限'),('购物',r'不考虑商超|配套不限')]:
        if re.search(pattern,text):
            for term in ((key,'小学','中学','幼儿园') if key=='教育' else (key,)):
                if term in groups:groups.remove(term)
            endings=('小学','中学','幼儿园') if key=='教育' else ('医院',) if key=='医疗' else ('超市','购物中心')
            exact=[name for name in exact if not name.endswith(endings)]
    hardness=amenity_hardness(context)
    hard_groups=[name for name in groups if hardness.get(name)=='hard']
    byid={p['property_id'].casefold():p['property_id'] for p in properties};ids=[]
    for candidate in byid:
        if re.search(r'(?<![A-Za-z0-9_-])'+re.escape(candidate)+r'(?![A-Za-z0-9_-])',text,re.I):ids.append(byid[candidate])
    unknown_ids=[m.group() for m in re.finditer(r'(?<![A-Za-z0-9_-])(?:P\d{3,}|R-[A-Za-z0-9_-]{4,})(?![A-Za-z0-9_-])',text,re.I) if m.group().casefold() not in byid]
    direct=re.search(r'(?:房源编号|房源ID|编号)[：:\s]*([A-Za-z0-9_-]+)',text,re.I)
    if direct and direct[1].casefold() not in byid:unknown_ids.append(direct[1])
    if not ids and not unknown_ids and re.search(r'这套|它的|该房源',text):
        for line in reversed(users[:-1]):
            ids=[byid[k] for k in byid if re.search(r'(?<![A-Za-z0-9_-])'+re.escape(k)+r'(?![A-Za-z0-9_-])',line,re.I)]
            if ids:break
    names=[]
    for row in catalog:
        if row['community_name'] and normal(row['community_name']) in normal(context):names.append(row['community_name'])
    names=list(dict.fromkeys(names));keys=[r['community_key'] for r in catalog if r['community_name'] in names and (not preferences.get('region') or r['region']==preferences['region'])]
    if not names and re.search(r'第[一二三四五六七八九\d]+个',text):
        number=re.search(r'第([一二三四五六七八九\d]+)个',text)[1];index=int(number) if number.isdigit() else '一二三四五六七八九'.index(number)+1
        for message in reversed(history):
            if message['role']!='assistant':continue
            choices=re.findall(r'^### (\d+)\. (.+?) ｜ (.+)$',message['content'],re.M)
            found=next(((name,region) for i,name,region in choices if int(i)==index),None)
            if choices:
                if found:
                    names=[found[0]];keys=[r['community_key'] for r in catalog if r['community_name']==found[0] and r['region']==found[1]]
                break
    missing_name=None
    if not names:
        m=re.search(r'(?:看看|查一下|找一下|查询|想看|推荐|只看)?([\u4e00-\u9fffA-Za-z0-9]{2,16}(?:花园|雅苑|公馆|新城|家园))(?:的|里|有没有|房源|小区|，|。|\s|$)',text)
        if m:
            value=re.split(r'看看|查一下|找一下|查询|想看|推荐|只看|帮我|我想',m[1])[-1]
            if value and value not in ('附近花园','周边公园'):missing_name=value
    communities_only=bool(re.search(r'先.{0,4}(?:看|找|查|推荐)小区|只.{0,3}(?:看|查|找)小区|不急.{0,3}(?:选|找)房|暂不.{0,3}(?:选|找)房',text))
    communities_only=communities_only or bool(re.search(r'(?:先|只).{0,24}(?:小区|社区)(?:[，。！？!?]|$)',text))
    if re.search(r'再.{0,6}(?:找|选|看)?房',text):communities_only=False
    questions=[]
    hard=parse_request(text)['explicit_preferences'];explicit_hard=any(k not in ('education_priority','prefer_near_metro','commute_destination') and v is not None for k,v in hard.items())
    # Soft-only amenities must not upgrade a hard-condition search into parallel;
    # an amenity without any soft marker is still treated as a requirement.
    soft_only=bool(groups) and not hard_groups and explicit_hard
    if soft_only:exact=[]
    route='property_details' if ids or unknown_ids else 'community_listings' if names or missing_name else 'parallel' if hard_groups and explicit_hard else 'listing_first' if explicit_hard and (groups or exact) else 'community_first' if groups or exact else 'listing_first'
    if communities_only and route=='parallel':route='community_first'
    if route in ('community_first','parallel') and not communities_only:
        if 'max_price_yuan' not in preferences and not re.search(r'预算不限|不限预算',context):questions.append('预算上限大概是多少？')
        if 'bedrooms' not in preferences and not re.search(r'户型不限|不限户型',context):questions.append('希望几室，还是户型不限？')
        if '教育' in hard_groups and not exact:questions.append('教育配套主要看幼儿园、小学还是中学？')
    if names and not keys:questions.append('指定小区与当前区域条件不一致，想调整区域还是换个小区？')
    return dict(route=route,query=context[-2000:],amenities=[] if soft_only else groups,exact_terms=exact,community_names=names,community_keys=list(dict.fromkeys(keys)),property_ids=list(dict.fromkeys(ids)),unknown_property_ids=list(dict.fromkeys(unknown_ids)),unknown_community=missing_name,communities_only=communities_only,questions=questions[:3],soft_amenities_only=soft_only)

def mismatches(p,prefs):
    checks=[('max_price_yuan',p['price_yuan']>prefs.get('max_price_yuan',p['price_yuan']),'总价超过预算'),('bedrooms',p['bedrooms']!=prefs.get('bedrooms',p['bedrooms']),'户型不符'),('min_floor',p['floor']<prefs.get('min_floor',p['floor']),'楼层低于要求'),('max_floor',p['floor']>prefs.get('max_floor',p['floor']),'楼层高于要求'),('region',p['region']!=prefs.get('region',p['region']),'区域不符'),('require_elevator',p['has_elevator']!=prefs.get('require_elevator',p['has_elevator']),'电梯条件不符'),('max_metro_distance_m',prefs.get('max_metro_distance_m') is not None and (p['metro_distance_m'] is None or p['metro_distance_m']>prefs['max_metro_distance_m']),'地铁距离不符或未录入')]
    for key,failed,message in [('min_area_sqm',p['area_sqm']<prefs.get('min_area_sqm',p['area_sqm']),'面积小于要求'),('max_area_sqm',p['area_sqm']>prefs.get('max_area_sqm',p['area_sqm']),'面积大于要求'),('orientation',prefs.get('orientation','') not in p['orientation'],'朝向不符'),('decoration',p['decoration']!=prefs.get('decoration',p['decoration']),'装修条件不符')]:checks.append((key,failed,message))
    return [message for key,failed,message in checks if key in prefs and failed]
