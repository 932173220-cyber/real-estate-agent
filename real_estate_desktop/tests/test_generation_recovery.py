import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from real_estate_desktop.model_client import ChatModelClient

def chunk(text=None,thought=None,tools=None,finish=None):
    return NS(choices=[NS(finish_reason=finish,delta=NS(content=text,reasoning_content=thought,tool_calls=tools))])

class Stream:
    def __init__(self,*chunks):self.chunks=chunks;self.close=AsyncMock()
    def __aiter__(self):
        async def items():
            for value in self.chunks:yield value
        return items()

def model_for(*streams,name='glm-5.3-flash'):
    model=object.__new__(ChatModelClient);model.model=name
    model.client=NS(chat=NS(completions=NS(create=AsyncMock(side_effect=list(streams)))))
    return model

class GenerationRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_default_budget_and_light_reasoning_for_supported_model(self):
        stream=Stream(chunk('完成',finish='stop'));model=model_for(stream)
        self.assertEqual((await model.generate([{'role':'user','content':'问题'}],[]))['content'],'完成')
        args=model.client.chat.completions.create.call_args.kwargs
        self.assertEqual(args['max_tokens'],8192);self.assertEqual(args['reasoning_effort'],'low');stream.close.assert_awaited_once()
    async def test_provider_specific_reasoning_is_not_sent_to_other_models(self):
        model=model_for(Stream(chunk('完成',finish='stop')),name='another-model')
        await model.generate([],[]);self.assertNotIn('reasoning_effort',model.client.chat.completions.create.call_args.kwargs)
    async def test_partial_text_is_discarded_before_a_single_larger_retry(self):
        first=Stream(chunk('未完成片段',thought='中间思考',finish='length'));second=Stream(chunk('完整回答',finish='stop'));model=model_for(first,second);shown=[]
        result=await model.generate([{'role':'user','content':'原始问题'}],[],on_chunk=shown.append)
        self.assertEqual(result['content'],'完整回答');self.assertEqual(shown,['完整回答'])
        calls=model.client.chat.completions.create.call_args_list
        self.assertEqual([c.kwargs['max_tokens'] for c in calls],[8192,16384]);self.assertEqual(calls[0].kwargs['messages'],calls[1].kwargs['messages'])
        first.close.assert_awaited_once();second.close.assert_awaited_once()
    async def test_partial_tool_arguments_are_never_returned_or_combined_with_retry(self):
        bad=NS(index=0,id='bad',function=NS(name='search_listings',arguments='{"bedrooms":'))
        good=NS(index=0,id='good',function=NS(name='search_listings',arguments='{"bedrooms":3}'))
        model=model_for(Stream(chunk(tools=[bad],finish='length')),Stream(chunk(tools=[good],finish='tool_calls')))
        result=await model.generate([],[])
        self.assertEqual(result['tool_calls'][0]['id'],'good');self.assertEqual(result['tool_calls'][0]['function']['arguments'],'{"bedrooms":3}')
    async def test_second_truncation_stops_without_unbounded_retry(self):
        model=model_for(Stream(chunk('半截',finish='length')),Stream(chunk('仍然半截',finish='length')))
        with self.assertRaisesRegex(ValueError,'仍未完成'):await model.generate([],[])
        self.assertEqual(model.client.chat.completions.create.call_count,2)
    async def test_input_context_limit_does_not_trigger_output_budget_retry(self):
        model=model_for(Stream(chunk(finish='model_context_window_exceeded')))
        with self.assertRaisesRegex(ValueError,'输入上下文'):await model.generate([],[])
        self.assertEqual(model.client.chat.completions.create.call_count,1)
    async def test_unfinished_stream_is_rejected_and_closed(self):
        stream=Stream(chunk('没有结束标记'));model=model_for(stream)
        with self.assertRaisesRegex(ValueError,'未正常完成'):await model.generate([],[])
        stream.close.assert_awaited_once();self.assertEqual(model.client.chat.completions.create.call_count,1)
    async def test_diagnostics_count_lengths_without_storing_raw_content(self):
        model=model_for(Stream(chunk('private answer',thought='private reasoning',finish='stop')))
        result=await model.generate([{'role':'user','content':'private input'}],[])
        self.assertEqual(result['reasoning_content'],'private reasoning')
        event=model.generation_events[0];self.assertEqual(event['answer_chars'],len('private answer'));self.assertEqual(event['finish_reason'],'stop')
        self.assertNotIn('private',str(event))
