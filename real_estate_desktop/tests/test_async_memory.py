import asyncio,tempfile,unittest
from pathlib import Path
from real_estate_desktop.config import Settings
from real_estate_desktop.runtime import Runtime
from real_estate_desktop.test_support import ScriptedModel

class AsyncMemory(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.settings=Settings(Path(self.temp.name),report_mode='explicit')
        self.r=Runtime(self.settings,model=ScriptedModel())
        self.cid=self.r.create_customer('自动记忆客户')['customer_id'];self.sid=self.r.new_session(self.cid)
    async def asyncTearDown(self):await self.r.close();self.temp.cleanup()
    async def drain(self):
        while self.r.background:await asyncio.gather(*tuple(self.r.background),return_exceptions=True)
    async def test_no_draft_turn_writes_preferences_without_confirmation(self):
        out=await self.r.chat('预算400万以内，三室，不要一楼',self.cid,self.sid)
        self.assertIsNone(out['draft_id'])
        await self.drain()
        self.assertEqual(self.r.profile(self.cid)['max_price_yuan'],4000000)
        self.assertEqual(self.r.profile(self.cid)['bedrooms'],3)
        self.assertTrue(self.r.preference_versions(self.cid))
        self.assertEqual(self.r.business.memory_jobs(self.r.broker,self.cid),[])

if __name__=='__main__':unittest.main()
