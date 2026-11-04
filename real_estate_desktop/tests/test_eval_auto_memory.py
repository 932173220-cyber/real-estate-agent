import tempfile,unittest
from pathlib import Path
from real_estate_desktop.config import Settings
from real_estate_desktop.runtime import Runtime
from real_estate_desktop.test_support import ScriptedModel
from real_estate_desktop.eval.scenarios import build_scenarios
from real_estate_desktop.eval.scenario_runner import ScenarioEvaluator
from real_estate_desktop.eval.scenario_scorers import score_scenario

class EvalAutoMemory(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.runtime=Runtime(Settings(Path(self.temp.name),report_mode='explicit'),model=ScriptedModel())
        self.evaluator=ScenarioEvaluator(self.runtime)
    async def asyncTearDown(self):await self.runtime.close();self.temp.cleanup()
    async def test_memory_category_contains_no_draft_auto_memory_samples_that_pass(self):
        samples=[row for row in build_scenarios(100) if row['category']=='customer_memory' and row.get('auto_memory')]
        for op in ('add','update'):
            sample=next(row for row in samples if row['op']==op)
            observed=await self.evaluator.run(sample)
            scored=score_scenario(sample,observed)
            self.assertTrue(scored['passed'],scored['reasons'])

if __name__=='__main__':unittest.main()
