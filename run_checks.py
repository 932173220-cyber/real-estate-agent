import hashlib,json,sys,unittest
from datetime import datetime
from pathlib import Path
base=Path(__file__).parent
source=base/'src' if (base/'src').is_dir() else base
sys.path.insert(0,str(source));sys.path.insert(0,str(base/'vendor'))
digest=hashlib.sha256()
for file in sorted((source/'real_estate_desktop').rglob('*.py')):
    digest.update(file.relative_to(source/'real_estate_desktop').as_posix().encode());digest.update(file.read_bytes())
fingerprint=digest.hexdigest()
base.joinpath('checks.json').write_text(json.dumps({'status':'running','source_digest':fingerprint}),encoding='utf-8')
suite=unittest.defaultTestLoader.discover(str(source/'real_estate_desktop/tests'))
result=unittest.TextTestRunner(verbosity=2).run(suite)
report={'at':datetime.now().isoformat(),'source_digest':fingerprint,'suite':'房地产助手本地工程与界面自动化检查','tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'passed':result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),'online_llm_evaluation':False,'notes':'本地功能/工程检查及脚本化模型接口测试，不能当作真实模型任务成功率。'}
Path(__file__).with_name('checks.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
sys.exit(0 if result.wasSuccessful() else 1)
