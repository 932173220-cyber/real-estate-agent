import json,unittest
from unittest.mock import AsyncMock
from real_estate_desktop.model_client import ChatModelClient

class ModelRouter(unittest.IsolatedAsyncioTestCase):
    async def test_router_uses_online_generation_and_excludes_internal_test_hint(self):
        model=object.__new__(ChatModelClient)
        result={'route':'community_first','query':'小学、医疗配套','questions':[],'exact_terms':[]}
        model.generate=AsyncMock(return_value={'role':'assistant','content':'','tool_calls':[{'id':'x','function':{'name':'choose_search_route','arguments':json.dumps(result,ensure_ascii=False)}}]})
        context={'current_message':'希望上学看病方便','customer_memory':{'preferences':{'max_price_yuan':4000000}},'conversation':[{'role':'user','content':'希望上学看病方便'}],'_host_plan':{'route':'listing_first'}}
        decision=await model.plan_search(context);self.assertEqual(decision['route'],'community_first')
        messages,tools=model.generate.call_args.args;sent=json.loads(messages[1]['content']);self.assertNotIn('_host_plan',sent);self.assertEqual(sent['customer_memory'],context['customer_memory']);self.assertEqual(tools[0]['function']['name'],'choose_search_route')
    async def test_malformed_tool_response_retries_without_unanswered_tool_calls(self):
        model=object.__new__(ChatModelClient);valid={'role':'assistant','content':'','tool_calls':[{'id':'good','function':{'name':'choose_search_route','arguments':'{"route":"listing_first","query":"三室","questions":[]}'}}]}
        model.generate=AsyncMock(side_effect=[{'role':'assistant','content':'','tool_calls':[{'id':'bad','function':{'name':'unavailable','arguments':'{}'}}]},valid])
        out=await model.plan_search({'current_message':'三室'});self.assertEqual(out['route'],'listing_first');self.assertEqual(model.generate.call_count,2)
        self.assertTrue(all('tool_calls' not in m for m in model.generate.call_args.args[0]))
