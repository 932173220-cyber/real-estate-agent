"""Streaming model calls with bounded generation recovery; tools execute in the host."""
from __future__ import annotations

import asyncio,logging,time
from openai import AsyncOpenAI

class GenerationTruncated(ValueError):pass


class ChatModelClient:
    def __init__(self, settings, *, client=None):
        self.model = settings.chat_model
        # httpx2's zstd decoder in this dependency set rejects compressed
        # bodies, so the client asks for identity encoding; streaming text is
        # not large enough for gzip to matter here.
        self.client = client or AsyncOpenAI(api_key=settings.chat_api_key,
            base_url=settings.chat_base_url, timeout=45, max_retries=1,
            default_headers={'Accept-Encoding': 'identity'})

    async def generate(self, messages, tools, *, allow_tools=True, on_chunk=None):
        # Discard incomplete text/tool arguments; no host tool is executed by this method.
        for attempt,budget in enumerate((8192,16384),1):
            try:
                async with asyncio.timeout(120):
                    return await self._generate_once(messages,tools,allow_tools=allow_tools,on_chunk=on_chunk,budget=budget,attempt=attempt)
            except GenerationTruncated:
                if attempt==2:raise ValueError('模型在扩大生成预算后仍未完成，请缩短任务后重试。') from None

    async def _generate_once(self,messages,tools,*,allow_tools,on_chunk,budget,attempt):
        options = {"model": self.model, "messages": messages, "stream": True,
                   "temperature": 0.2, "max_tokens": budget}
        if self.model.lower().startswith('glm-5.3'):options['reasoning_effort']='low'
        if tools:
            options.update(tools=tools, tool_choice="auto" if allow_tools else "none")
        parts, reasoning, calls = [], [], {}
        finish=None;stream=None;started=time.monotonic()
        event={'attempt':attempt,'budget':budget,'reasoning_effort':options.get('reasoning_effort'),'input_chars':sum(len(m.get('content') or '') for m in messages)}
        events=getattr(self,'generation_events',[]);events.append(event);self.generation_events=events[-100:]
        try:
            stream = await self.client.chat.completions.create(**options)
            async for chunk in stream:
                usage=getattr(chunk,'usage',None)
                if usage is not None:
                    event['usage']={key:getattr(usage,key,None) for key in ('prompt_tokens','completion_tokens','total_tokens')}
                if not chunk.choices:
                    continue
                if getattr(chunk.choices[0],'finish_reason',None):finish=chunk.choices[0].finish_reason
                delta = chunk.choices[0].delta
                if delta.content:
                    parts.append(delta.content)
                if getattr(delta, "reasoning_content", None):
                    reasoning.append(delta.reasoning_content)
                for call in delta.tool_calls or []:
                    if call.index not in calls:
                        calls[call.index] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                    item = calls[call.index]
                    if call.id:
                        item["id"] = call.id
                    if call.function:
                        if call.function.name:
                            item["function"]["name"] += call.function.name
                        if call.function.arguments:
                            item["function"]["arguments"] += call.function.arguments
        except BaseException as error:
            event['error_type']=type(error).__name__;raise
        finally:
            if stream is not None:await stream.close()
            event.update(finish_reason=finish,reasoning_chars=sum(map(len,reasoning)),answer_chars=sum(map(len,parts)),tool_argument_chars=sum(len(v['function']['arguments']) for v in calls.values()),elapsed_seconds=round(time.monotonic()-started,3))
            logging.getLogger(__name__).info('model_generation %s',event)
        message = {"role": "assistant", "content": "".join(parts)}
        if finish=='length':raise GenerationTruncated('模型输出被截断。')
        if finish=='model_context_window_exceeded':raise ValueError('输入上下文超过模型窗口，请减少本次提供的资料。')
        if finish not in ('stop','tool_calls'):raise ValueError('模型未正常完成本次生成，请稍后重试。')
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if calls:
            if not allow_tools:
                raise ValueError("模型在工具调用上限后仍请求工具")
            tool_calls = [calls[index] for index in sorted(calls)]
            ids = [item["id"] for item in tool_calls]
            if len(tool_calls) > 8 or not all(ids) or len(set(ids)) != len(ids):
                raise ValueError("模型工具调用格式不正确")
            message["tool_calls"] = tool_calls
        elif not message["content"].strip():
            raise ValueError("模型未返回有效回答")
        elif on_chunk:
            # Do not show intermediate tool-intent text as the final answer.
            for part in parts:
                on_chunk(part)
        return message

    async def aclose(self):
        await self.client.close()
    async def extract_json(self,prompt,data,schema):
        """Dedicated LLM extraction/comparison call; the component performs strict local validation."""
        import asyncio,json
        parts=[];truncated=False
        reasoning_options={'reasoning_effort':'low'} if self.model.lower().startswith('glm-5.3') else {}
        messages=[{'role':'system','content':prompt+'\n输出必须符合以下JSON Schema：'+json.dumps(schema,ensure_ascii=False)},
            {'role':'user','content':json.dumps(data,ensure_ascii=False)}]
        options={'model':self.model,'stream':True,'temperature':0.1,'max_tokens':8192,**reasoning_options,'messages':messages}
        async with asyncio.timeout(120):
            client=self.client.with_options(timeout=90,max_retries=0)
            try:
                stream=await client.chat.completions.create(**options,response_format={'type':'json_object'})
            except Exception as error:
                # Some OpenAI-compatible servers reject response_format; the
                # schema is already in the prompt, so retry once without it.
                if type(error).__name__!='BadRequestError':raise
                stream=await client.chat.completions.create(**options)
            try:
                async for chunk in stream:
                    if not chunk.choices:continue
                    truncated=truncated or chunk.choices[0].finish_reason=='length'
                    if chunk.choices[0].delta.content:parts.append(chunk.choices[0].delta.content)
            finally:await stream.close()
        if truncated or not parts:raise ValueError('模型结构化输出未完成，请稍后重试。')
        try:return json.loads(''.join(parts))
        except ValueError as error:raise ValueError('模型返回的结构化内容不是有效JSON，请稍后重试。') from error
    async def memory_json(self,prompt,data,schema):
        return await self.extract_json(prompt,data,schema)
    async def plan_search(self,context):
        from .search_planning import RouteDecision
        data={k:v for k,v in context.items() if not k.startswith('_')}
        import json
        prompt='''根据当前用户消息、当前客户长期记忆和同一客户的本轮对话，调用choose_search_route决定房产检索路径。
community_first：本次仅提出学校、医疗、购物、环境等配套要求，先RAG找小区，再按客户已知硬条件筛房。记忆里已有预算不等于本次明确说了房屋条件。
listing_first：本次只说预算、户型、面积、楼层等房屋条件，先查房，再查对应小区资料。
parallel：本次房屋条件和配套需求同时存在，或在前面配套需求上补充房屋条件，可并行检索后按小区取交集。
property_details：用户指定准确房源编号，查房源及所属小区。community_listings：用户指定小区名称，查该小区房源。
用户说先看/只看小区时用community_first。不确定的具体学校名称、预算或户型要追问，不编造。未量化的“附近”等仅作为软偏好，不猜距离阈值。用户要求直接推荐且硬条件已足够时，不因可选偏好重复追问。当前明确要求优先于旧偏好。query只改写用户实际表达的需求，不增加未提及的学校/配套名称。exact_terms填写用户明确指定的学校/医院等名字或必须核验的资格词，逐字取自用户原文或已确认的客户偏好；通用“学校、医院”不是名字，软偏好不作为硬门槛，没有则返回空数组。questions最多3个，长期记忆已明确或用户明确不限的条件不要重复追问。
用户本次明确表达个人居住配套偏好时，preference_topics可提取小学、中学、幼儿园、教育、医疗、购物、公园、安静、交通或用户明确说出的设施名称，并给出preference_quote原文短引。只是在问某套房有什么配套，不算偏好；没有明确表达时省略这两个字段。明确取消配套限制可返回空数组和原文短引。它们只是待确认的记忆更新，不能直接写入。不更改客户身份，不输出SQL，不直接推荐或保存。必须通过工具提交结构化决定。'''
        prompt+='\n具体学校、医院等名称必须完整复制原文，不得缩短名称、删除前缀或改写成相反条件。仅提交工具决定，避免展开报告。'
        prompt+='\n用户原文里的预算、户型、楼层、面积、朝向、装修、区域、电梯、地铁距离等硬条件，如果宿主正则可能漏识别（例如“两房”“四百万上下”“首付加贷款”），请放进 constraints：field 用字段名，value 保留原文单位和写法，quote 必须逐字取自本条用户原文。需要换算或推断的值（如首付+贷款推总价）设 inferred=true，宿主会先向用户确认。没有原文依据的字段不要提交。'
        tool={'type':'function','function':{'name':'choose_search_route','description':'基于客户记忆和当前对话选择检索分支及必要追问。','parameters':RouteDecision.model_json_schema()}}
        messages=[{'role':'system','content':prompt},{'role':'user','content':json.dumps(data,ensure_ascii=False)}]
        for attempt in range(2):
            response=await self.generate(messages,[tool]);calls=response.get('tool_calls',[])
            if len(calls)==1 and calls[0].get('function',{}).get('name')=='choose_search_route':return RouteDecision.model_validate_json(calls[0]['function']['arguments']).model_dump(exclude_unset=True)
            messages.extend([{'role':'assistant','content':response.get('content') or '尚未取得有效检索计划。'},{'role':'user','content':'请调用choose_search_route提交决定，不要直接回答。'}])
        raise ValueError('模型未返回可校验的检索计划，原始需求已保留，请补充信息后重试。')
