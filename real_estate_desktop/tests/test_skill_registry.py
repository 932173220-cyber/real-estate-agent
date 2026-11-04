import unittest
from pathlib import Path

from real_estate_desktop.skill_registry import Skill, SkillRegistry, registry
from real_estate_desktop.tools import Context


ROOT = Path(__file__).resolve().parent.parent


class Registry(unittest.TestCase):
    def build(self):
        return SkillRegistry(ROOT / 'skills')

    def test_builtin_skills_are_loaded_on_demand(self):
        reg = self.build()
        reg.register(Skill('demo', '演示', ('search_listings',), path=ROOT / 'skills/listing-search/SKILL.md'))
        skill = reg.get('demo')
        self.assertIn('name: listing-search', skill.instructions)
        self.assertIs(skill.instance(), skill)  # singleton reuse

    def test_registry_hands_out_one_instance_per_skill(self):
        reg = self.build()
        reg.register(Skill('demo', '演示', path=ROOT / 'skills/listing-search/SKILL.md'))
        first, second = reg.get('demo'), reg.get('demo')
        self.assertIs(first, second)
        self.assertIs(first, first.instance())
        self.assertEqual(len(reg.names()), 1)

    def test_duplicate_registration_is_rejected_unless_replaced(self):
        reg = self.build()
        reg.register(Skill('demo', '一', path=ROOT / 'skills/listing-search/SKILL.md'))
        with self.assertRaises(ValueError):
            reg.register(Skill('demo', '二', path=ROOT / 'skills/listing-search/SKILL.md'))
        reg.register(Skill('demo', '二', path=ROOT / 'skills/listing-search/SKILL.md'), replace=True)
        self.assertEqual(reg.get('demo').description, '二')

    def test_unknown_skill_raises(self):
        with self.assertRaises(ValueError):
            self.build().get('nope')

    def test_tool_scope_includes_core_tools_only(self):
        reg = self.build()
        reg.register(Skill('demo', '演示', ('search_listings',), path=ROOT / 'skills/listing-search/SKILL.md'))
        self.assertEqual(reg.tools_for([]), {'think', 'load_skill'})
        self.assertEqual(reg.tools_for(['demo']), {'think', 'load_skill', 'search_listings'})


class ProgressiveDisclosure(unittest.TestCase):
    def test_tool_schemas_appear_only_after_activation(self):
        from real_estate_desktop.test_support import ScriptedModel
        from real_estate_desktop.config import Settings
        from real_estate_desktop.runtime import Runtime
        import tempfile, asyncio

        async def scenario():
            with tempfile.TemporaryDirectory() as folder:
                runtime = Runtime(Settings(Path(folder)), model=ScriptedModel())
                try:
                    ctx = Context(runtime.broker, 'c', 's', 'req')
                    before = {row['function']['name'] for row in runtime.tools.definitions(ctx)}
                    self.assertNotIn('draft_recommendation', before)
                    runtime.tools.activate(ctx, 'recommendation')
                    after = {row['function']['name'] for row in runtime.tools.definitions(ctx)}
                    self.assertIn('draft_recommendation', after)
                    self.assertIn('load_skill', after)
                    self.assertIn('think', after)
                finally:
                    await runtime.close()

        asyncio.run(scenario())

    def test_load_skill_tool_activates_the_skill(self):
        from real_estate_desktop.test_support import ScriptedModel
        from real_estate_desktop.config import Settings
        from real_estate_desktop.runtime import Runtime
        import tempfile, asyncio

        async def scenario():
            with tempfile.TemporaryDirectory() as folder:
                runtime = Runtime(Settings(Path(folder)), model=ScriptedModel())
                try:
                    cid = runtime.create_customer('技能客户')['customer_id']
                    sid = runtime.new_session(cid)
                    ctx = Context(runtime.broker, cid, sid, 'req')
                    result = await runtime.tools.execute('load_skill', {'name': 'recommendation'}, ctx)
                    self.assertEqual(result['status'], 'ok')
                    self.assertIn('recommendation', runtime.tools.active_skills(ctx))
                    self.assertEqual(result['data']['tools'], ['draft_recommendation'])
                finally:
                    await runtime.close()

        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
