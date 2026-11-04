import json,unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock
from real_estate_desktop.model_client import ChatModelClient

class Stream:
    def __init__(self,parts,finish='stop'):self.parts=parts;self.finish=finish;self.close=AsyncMock()
    def __aiter__(self):
        async def chunks():
            for part in self.parts:yield NS(choices=[NS(finish_reason=None,delta=NS(content=part))])
            yield NS(choices=[NS(finish_reason=self.finish,delta=NS(content=None))])
        return chunks()

class MemoryModel(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_json_is_assembled_and_closed(self):
        stream=Stream(['{"decisions":','[],"retention_decisions":[]}'])
        create=AsyncMock(return_value=stream);client=NS(chat=NS(completions=NS(create=create)));client.with_options=Mock(return_value=client)
        model=object.__new__(ChatModelClient);model.client=client;model.model='test'
        result=await model.memory_json('prompt',{'source':'原文'},{'type':'object'})
        self.assertEqual(result,{'decisions':[],'retention_decisions':[]});stream.close.assert_awaited_once()
        self.assertTrue(create.call_args.kwargs['stream']);self.assertEqual(client.with_options.call_args.kwargs['max_retries'],0)
    async def test_truncated_json_cannot_be_accepted_as_memory(self):
        stream=Stream(['{}'],finish='length');client=NS(chat=NS(completions=NS(create=AsyncMock(return_value=stream))));client.with_options=lambda **kwargs:client
        model=object.__new__(ChatModelClient);model.client=client;model.model='test'
        with self.assertRaises(ValueError):await model.memory_json('prompt',{},{} )
        stream.close.assert_awaited_once()
