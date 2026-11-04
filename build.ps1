$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$modelFlag = @()
if (Test-Path "$PSScriptRoot\models\bge-small-zh-v1.5") {
  $modelFlag = @('--add-data', "$PSScriptRoot\models\bge-small-zh-v1.5;models/bge-small-zh-v1.5")
}
python -m PyInstaller --noconfirm --onefile --windowed --name '房产经纪智能助手' `
  --icon "$PSScriptRoot\assets\property-assistant.ico" `
  --paths "$PSScriptRoot" --paths "$PSScriptRoot\vendor" `
  --collect-submodules langgraph --copy-metadata langgraph `
  --copy-metadata langgraph-checkpoint --copy-metadata langgraph-checkpoint-sqlite --copy-metadata langchain-core `
  --collect-submodules chromadb --copy-metadata chromadb --hidden-import chromadb.telemetry.product.posthog `
  --collect-submodules sentence_transformers --copy-metadata sentence_transformers --copy-metadata transformers `
  --add-data "$PSScriptRoot\real_estate_desktop\skills;real_estate_desktop/skills" `
  --add-data "$PSScriptRoot\assets;assets" --hidden-import pypdf --hidden-import jieba `
  --add-data "$PSScriptRoot\vendor\agent_memory\resources;agent_memory/resources" `
  @modelFlag `
  --exclude-module agentify --exclude-module gradio --exclude-module fastapi `
  --exclude-module pandas --exclude-module matplotlib `
  "$PSScriptRoot\property_assistant.py"
if ($LASTEXITCODE -ne 0) { throw '程序打包失败，请查看上面的错误。' }
