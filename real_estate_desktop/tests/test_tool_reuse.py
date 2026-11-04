import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from real_estate_desktop.config import Settings
from real_estate_desktop.runtime import Runtime
from real_estate_desktop.test_support import ScriptedModel
from real_estate_desktop.tools import Context

class Reuse(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.r=Runtime(Settings(Path(self.temp.name)),model=ScriptedModel())
        self.cid=self.r.create_customer('工具复用客户')['customer_id'];self.sid=self.r.new_session(self.cid)
        self.ctx=Context(self.r.broker,self.cid,self.sid,'reuse-request')
    async def asyncTearDown(self):await self.r.close();self.temp.cleanup()
    async def call(self,tool,args):return await self.r.tools.execute(tool,args,self.ctx)
    async def test_same_read_only_arguments_are_reused_without_hitting_the_database(self):
        args={'max_price_yuan':4000000,'bedrooms':3,'min_floor':2}
        first=await self.call('search_listings',args)
        self.assertIn(first['status'],('ok','empty'));self.assertNotIn('reused',first)
        with patch.object(self.r.business,'search',wraps=self.r.business.search) as search:
            second=await self.call('search_listings',args)
        self.assertTrue(second['reused']);self.assertEqual(search.call_count,0)
        self.assertEqual(second['data'],first['data'])
        self.assertEqual([row['status'] for row in self.r.tools.trace(self.ctx)],['ok','reused'])
    async def test_changed_arguments_execute_again(self):
        await self.call('search_listings',{'max_price_yuan':4000000,'bedrooms':3})
        with patch.object(self.r.business,'search',wraps=self.r.business.search) as search:
            out=await self.call('search_listings',{'max_price_yuan':3500000,'bedrooms':3})
        self.assertNotIn('reused',out);self.assertEqual(search.call_count,1)
    async def test_refresh_bypasses_reuse_and_errors_are_not_cached(self):
        first=await self.call('query_customer_memory',{'query':'偏好','phase':'light'})
        second=await self.call('query_customer_memory',{'query':'偏好','phase':'light'})
        self.assertTrue(second['reused'])
        refreshed=await self.call('query_customer_memory',{'query':'偏好','phase':'light','refresh':True})
        self.assertNotIn('reused',refreshed)
        invalid=await self.call('get_property_details',{'property_id':''});invalid_again=await self.call('get_property_details',{'property_id':''})
        self.assertEqual(invalid['status'],'error');self.assertEqual(invalid_again['status'],'error');self.assertNotIn('reused',invalid_again)

if __name__=='__main__':unittest.main()
