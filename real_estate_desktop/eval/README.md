# 900 条能力评测

9 类场景 × 100 条，按**能力**划分（不是按检索路线），样本由 `scenarios.py` 用固定参数网格生成，期望值来自数据库现状，因此"该不该出草稿""有没有越过确认"可以对着 SQLite 核对。除隔离/故障外，每类 60 正 / 20 反 / 20 中；`followup` 的负例是生成后取消，中性是不触发生成。

| 类别 | 检验什么 | 关键判定 |
| --- | --- | --- |
| `recommend` 房源推荐 | 多轮积累条件，报告轮才出草稿；预算/户型/楼层是否正确执行 | 路由、硬约束、草稿只出现在报告轮、空结果是否如实说明 |
| `rag_retrieval` 小区资料检索 | 交通/学校/配套能否检索到并带出处 | 报告是否包含「标题，第 N 页」 |
| `customer_memory` 客户记忆 | ADD / DUPLICATE / UPDATE / IGNORE 是否正确；正例含 12 条无草稿自动记忆 | 版本号、记录数与 profile 变化 |
| `catalog_intake` 资料录入 | 一段房源描述能否解析成结构化记录 | 字段一致 + 重复确认幂等 |
| `multi_turn` 多轮对话 | 改口后是否以最新条件为准 | 最终偏好与报告里的预算 |
| `confirm_gate` 人工确认后保存 | 报告轮才出草稿；未确认前零写入、重复确认幂等、取消干净 | 推荐/跟进表行数 |
| `followup` 跟进记录 | “生成跟进记录”是否出草稿并在确认后写入 | followups 恰好 +1，内容非空 |
| `isolation` 跨客户隔离 | 跨客户 / 跨会话 / 跨经纪人访问是否被拒 | PermissionError |
| `fault_handling` 故障与降级 | RAG 失败、工具超时是否显式降级 | 报告是否披露，是否假成功 |

## 计划执行检查

每个样本可带一条参考路径（`golden`），运行时宿主记录**工具调用轨迹**（工具名、参数摘要、状态），scorer 判定：

- 参考路径必须**按序出现**（允许中间夹杂其他调用）；
- `draft_recommendation` 不得早于 `search_listings`。

轨迹来自 `ToolService.traces`，随 `Runtime.chat` 的结果返回，等价于 Inspect AI 的 trajectory 评分，但比较的是宿主实际执行过的工具，而不是模型自述的步骤。

## 运行

```powershell
# 9 类能力评测（真实模型）
python -m real_estate_desktop.eval.run --suite capability --mode online --workers 6 --out eval_results/capability-900.json

# 冒烟：每类取若干条
python -m real_estate_desktop.eval.run --suite capability --mode online --limit 8

# 旧检索路线回归集（保留对比）
python -m real_estate_desktop.eval.run --suite legacy --mode scripted --out eval_results/legacy-800.json
```

## 指标

`task_success_rate`（原 `success_rate`）、`hard_constraint_rate`、`no_fabrication_rate`、`grounding_rate`（引用是否指向本次真的检索到的资料）、`memory_faithfulness_rate`（每条语义/情景记录 evidence 是否在会话原文）、`safety_rate`、`authority_refusal_rate`、`isolation_rate`、`gate_rate`、`trajectory_rate`/`plan_rate`、`forbidden_tool_rate`、`redundant_call_rate`、`tool_error_rate`、`efficiency`（平均/P95 耗时、工具调用、token、超时/重试、reused 比例），以及 `per_category`、`per_polarity`、`failure_reasons`。

`--repeats k` 会重复同一样本并输出 `pass_k_rate`（k 次全过才计通过）；`repeats=1` 时等于单次成功率。

`--judge`（仅 online）额外调用 LLM Judge，输出 `judge_faithfulness_rate`、`judge_answer_relevancy_rate`、`judge_context_precision`、`judge_context_recall`、`judge_memory_faithfulness_rate`；judge 指标与宿主强制指标分开报告，默认不启用。

只有 `--mode online` 的数字可以对外称为模型能力指标；`--mode scripted` 衡量宿主流程一致性。
