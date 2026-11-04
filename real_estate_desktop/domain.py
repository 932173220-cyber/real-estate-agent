from __future__ import annotations
import hashlib,re
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field

def customer_code(customer_id):
    """Stable presentation code; database identities and their relationships stay intact."""
    return 'C-'+hashlib.sha256(customer_id.encode('utf-8')).hexdigest()[:10].upper()

class Schema(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,str_strip_whitespace=True)

class Preferences(Schema):
    max_price_yuan:int|None=Field(default=None,ge=1,le=1000000000)
    bedrooms:int|None=Field(default=None,ge=1,le=10)
    min_area_sqm:float|None=Field(default=None,gt=0)
    max_area_sqm:float|None=Field(default=None,gt=0)
    orientation:str|None=None
    decoration:str|None=None
    min_floor:int|None=Field(default=None,ge=-2,le=100)
    max_floor:int|None=Field(default=None,ge=-2,le=100)
    region:str|None=None
    require_elevator:bool|None=None
    max_metro_distance_m:int|None=Field(default=None,ge=0,le=50000)
    prefer_near_metro:bool|None=None
    education_priority:bool|None=None
    commute_destination:str|None=None
    amenity_preferences:list[str]|None=Field(default=None,max_length=10)

class ListingQuery(Preferences):
    community_keys:list[str]|None=Field(default=None,max_length=30)
    property_ids:list[str]|None=Field(default=None,max_length=20)
    limit:int=Field(default=8,ge=1,le=20)

class CommunitySearchArgs(Schema):
    query:str=Field(min_length=1,max_length=2000)
    region:str|None=None
    limit:int=Field(default=6,ge=1,le=12)

class PropertyDetailsArgs(Schema):property_id:str=Field(min_length=1,max_length=80)

class Property(Schema):
    property_id:str=Field(min_length=1,max_length=80)
    community_key:str=Field(min_length=1,max_length=80)
    community_name:str=Field(min_length=1,max_length=100)
    region:str
    price_yuan:int=Field(gt=0)
    area_sqm:float=Field(gt=0)
    bedrooms:int=Field(ge=1,le=10)
    floor:int=Field(ge=-2,le=100)
    total_floors:int=Field(ge=1,le=100)
    orientation:str='未提供'
    decoration:str='未提供'
    building_age:int|None=Field(default=None,ge=0,le=150)
    has_elevator:bool
    metro_distance_m:int|None=Field(default=None,ge=0)
    intro:str=Field(max_length=500)
    is_demo:bool=True
    source:str='手工录入'

class DraftArgs(Schema):
    property_ids:list[str]=Field(default_factory=list,max_length=8)
    analysis:str=Field(default='',max_length=12000)

class FollowUpViewed(Schema):
    property_id:str=Field(min_length=1,max_length=80)
    feedback:str=Field(default='',max_length=200)

class DraftRevision(Schema):
    """Model-picked selection for a pending draft; the host rebuilds the report."""
    property_ids:list[str]=Field(default_factory=list,max_length=8)
    note:str=Field(default='',max_length=500)

class FollowUpDraft(Schema):
    """Model-extracted follow-up note; the host validates every field before rendering."""
    demand_timeline:list[str]=Field(default_factory=list,max_length=12)
    viewed:list[FollowUpViewed]=Field(default_factory=list,max_length=12)
    open_items:list[str]=Field(default_factory=list,max_length=12)
    summary:str=Field(min_length=1,max_length=1200)

PROPERTY_ID=re.compile(r'(?<![A-Za-z0-9_-])(?:P\d{3,}|[A-Z]{1,3}\d?-\d{2})(?![A-Za-z0-9_-])')

class AreaArgs(Schema):
    community_keys:list[str]=Field(min_length=1,max_length=30)
    query:str=Field(min_length=1,max_length=2000)

class ThinkArgs(Schema):
    thought:str=Field(min_length=1,max_length=500)
    next:str=Field(default='',max_length=80)

class MemoryArgs(Schema):
    query:str=Field(min_length=1,max_length=2000)
    refresh:bool=False
    phase:Literal['light','full']='full'
class SkillArgs(Schema):name:str=Field(min_length=1,max_length=80)

def chinese_number(text):
    if re.fullmatch(r'\d+(?:\.\d+)?',text):return float(text)
    digits={'零':0,'〇':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    total=number=0
    for char in text:
        if char in digits:number=digits[char]
        elif char in {'十','百','千'}:
            total+=(number or 1)*{'十':10,'百':100,'千':1000}[char];number=0
        else:raise ValueError('无法识别金额')
    return total+number

def parse_request(text):
    result={};number=r'[\d.零〇一二两三四五六七八九十百千]+'
    matches=list(re.finditer(r'(?:预算|总价|不超过|最多|最高|改为|改成|调整为)[^\d零一二两三四五六七八九十百千]{0,5}('+number+r')\s*(万元|万|元)',text))
    matches+=list(re.finditer(r'('+number+r')\s*(万元|万|元)\s*(?:以内|以下|封顶)',text));match=max(matches,key=lambda m:m.end()) if matches else None
    if match:result['max_price_yuan']=int(chinese_number(match[1])*(10000 if match[2] in ('万','万元') else 1))
    match=re.search(r'(\d|一|二|两|三|四|五)\s*(?:室|居)',text)
    if match:result['bedrooms']=int(chinese_number(match[1]))
    match=re.search(r'(?:面积)?\s*(\d+(?:\.\d+)?)\s*(?:平米|平方米|㎡|平)(?:以上|起|及以上)',text)
    if match:result['min_area_sqm']=float(match[1])
    match=re.search(r'(?:面积)?\s*(\d+(?:\.\d+)?)\s*(?:平米|平方米|㎡|平)(?:以内|以下|及以下)',text)
    if match:result['max_area_sqm']=float(match[1])
    match=re.search(r'(?:朝向|朝)[：:\s]*([东西南北]{1,2})',text)
    if match:result['orientation']=match[1]
    match=re.search(r'(?:要|要求|只看|装修)[：:\s]*(精装|简装|毛坯|豪装)',text)
    if match:result['decoration']=match[1]
    if re.search(r'不要一楼|不[选要]首层|排除一楼|非一楼|不接受一楼',text):result['min_floor']=2
    if re.search(r'(?<!不)接受一楼|一楼也?可以|(?<!不)可以一楼',text):result['min_floor']=1
    match=re.search(r'(?:最高|不超过|至多)(\d+)层',text)
    if match:result['max_floor']=int(match[1])
    if re.search(r'必须有电梯|需要电梯|要求电梯|要有电梯',text):result['require_elevator']=True
    if re.search(r'不需要电梯|电梯不限',text):result['require_elevator']=None
    match=re.search(r'地铁[^。；，]{0,9}?(\d+)\s*米(?:以内|内|以下)',text)
    if match:result['max_metro_distance_m']=int(match[1])
    match=re.search(r'地铁[^。；，]{0,6}?(?:不超过|最多|至多)(\d+)\s*米',text)
    if match:result['max_metro_distance_m']=int(match[1])
    if re.search(r'地铁近|离地铁近|靠近地铁|优先地铁|地铁优先',text):result['prefer_near_metro']=True
    if re.search(r'重视学区|看重学区|看重教育|重视教育|教育配套优先',text):result['education_priority']=True
    match=re.search(r'(?:区域|地区|想住|优先考虑)[：:\s]*([\u4e00-\u9fff]{2,10}区)',text)
    if match:result['region']=match[1]
    for phrase,keys in [('预算不限',('max_price_yuan',)),('楼层不限',('min_floor','max_floor')),('不限楼层',('min_floor','max_floor')),('户型不限',('bedrooms',)),('区域不限',('region',))]:
        if phrase in text:
            for key in keys:result[key]=None
    result=Preferences.model_validate(result).model_dump(exclude_unset=True)
    intent='recommend'
    if re.search(r'(?:生成|整理|写|做|出)[^。！？]{0,6}(?:跟进记录|跟进)',text) and not any(v in text for v in ('推荐','找房','选房')):intent='followup'
    elif re.search(r'(?:生成|出|整理|做|写|来)[^。！？]{0,6}(?:推荐)?(?:报告|方案)|整理一下|整理成.{0,4}报告',text):intent='report'
    elif any(v in text for v in ('记录跟进','保存跟进','添加跟进','跟进记录')) and not any(v in text for v in ('推荐','找房','选房')):intent='follow_up'
    elif any(v in text for v in ('回顾偏好','查看偏好','之前的偏好','客户画像','回顾客户','历史跟进','查看客户记忆','回顾购房经历')):intent='review'
    elif any(v in text for v in ('只记住','只更新偏好','仅更新偏好','记住客户','记录客户背景','更新客户资料','补充客户背景')):intent='memory'
    no_search=bool(re.search(r'不要查询|不查房源|暂不找房|仅更新偏好|只更新偏好',text))
    if no_search and intent=='recommend':intent='memory' if result else 'review'
    if re.search(r'(?:刷新|重新读取|重新加载|重新召回).{0,8}(?:记忆|偏好)',text):intent='review'
    return dict(explicit_preferences=result,intent=intent,prepare_save=not bool(re.search(r'不保存|不要保存|不用保存|无需保存|不要提交|暂不提交|只看看|只查询',text)),report_requested=bool(re.search(r'(?:生成|出|整理|做|写|来)[^。！？]{0,6}(?:推荐)?(?:报告|方案)|整理一下|整理成.{0,4}报告',text)))

def merge_preferences(memory,explicit):
    merged={**memory,**explicit}
    if merged.get('min_floor') is not None and merged.get('max_floor') is not None and merged['min_floor']>merged['max_floor']:
        raise ValueError('楼层上下限冲突，请明确调整客户的楼层条件。')
    if merged.get('min_area_sqm') is not None and merged.get('max_area_sqm') is not None and merged['min_area_sqm']>merged['max_area_sqm']:raise ValueError('面积上下限冲突，请明确调整客户的面积条件。')
    return Preferences.model_validate(merged).model_dump(exclude_none=True)

