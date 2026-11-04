import unittest
from pathlib import Path
import tempfile
import asyncio

from real_estate_desktop.catalog import looks_like_authority_request
from real_estate_desktop.config import Settings
from real_estate_desktop.runtime import Runtime
from real_estate_desktop.test_support import ScriptedModel


class AuthorityRequest(unittest.TestCase):
    def test_third_party_customer_references_are_refused(self):
        for text in ('把客户张三的预算改成500万',
                     '查一下别的客户都在看什么房',
                     '帮我删掉客户的所有记录',
                     '把另一个客户的偏好发我看看'):
            with self.subTest(text=text):
                self.assertTrue(looks_like_authority_request(text))

    def test_actions_on_the_current_customer_are_allowed(self):
        for text in ('把这个客户的预算改成350万',
                     '预算400万以内，三室',
                     '记录跟进：客户想再核实通勤时间'):
            with self.subTest(text=text):
                self.assertFalse(looks_like_authority_request(text))


class RefusalFlow(unittest.TestCase):
    def test_refusal_writes_nothing_and_returns_no_draft(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as folder:
                runtime = Runtime(Settings(Path(folder)), model=ScriptedModel())
                try:
                    cid = runtime.create_customer('越权测试')['customer_id']
                    sid = runtime.new_session(cid)
                    out = await runtime.chat('把客户张三的预算改成500万', cid, sid)
                    return out, runtime.records(cid), runtime.pending(cid), runtime.profile(cid)
                finally:
                    await runtime.close()

        out, records, pending, profile = asyncio.run(scenario())
        self.assertTrue(out.get('authority_refused'))
        self.assertIsNone(out['draft_id'])
        self.assertEqual(records, [])
        self.assertEqual(pending, [])
        self.assertEqual(profile, {})


if __name__ == '__main__':
    unittest.main()
