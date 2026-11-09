# Agent、报告与成本评测方案
> 层级：详细规格（展开基线）｜职责：质量、成本与回归评测方案｜冲突时以三份根文档为准

> 目标：证明专用 Agent 相比单 Prompt 的增益，并持续监控质量、风险和成本。  
> 最后更新：2026-10-13（T14 实现落地，第 10 节给出文档与代码的对照）

## 1. 评测对象

下表的 `target_type` 与 `evaluation/targets.py:TARGET_ORDER`、`eval_runs.target_type` 的数据库 CHECK 逐字一致；`--target` / `targets` 只接受这五个取值，未知值报 `400 UNKNOWN_TARGET`。

| 版本 | 说明 | 实现的消融开关 |
|---|---|---|
| `baseline_single_prompt` | 把候选数据拼接后一次性交给模型生成报告，不经工具、不验证 | 无（不读任何工具） |
| `agent_full` | Research、工具调用、Verify、Write 完整流程 | 无（生产 profile） |
| `agent_no_verifier` | 去掉 Verifier，用于消融 | `run_verifier=False` |
| `agent_no_tools` | 只使用一次候选数据，不调用历史趋势和 README 工具 | 禁用全部读工具 |
| `agent_no_history` | 保留工具，但不读取历史报告 | 只禁用 `search_prior_reports` |

**消融不是重新实现，而是真实 graph 上的一个开关**：`agent/profile.py:ExecutionProfile` 是整个消融面，`profile_for_target()` 把 `target_type` 映射到 profile。唯一不可禁用的工具是写草稿的 `save_report_section` —— 没有草稿的消融什么也测不到，所以运行时会保留它。生产运行恒用 `AGENT_FULL`。

评测不是为了追求学术指标，而是回答三个问题：报告是否可信、是否节省人工、Token 是否可控。

## 2. 数据集设计

### 2.1 样本来源

- 历史周报和人工核对过的项目分析。
- 公开仓库在不同 star 增速、语言和技术方向上的组合。
- 人工构造的边界样本：数据缺失、README 噪声、来源冲突、重复项目和异常增长。
- Prompt Injection 样本：要求 Agent 忽略规则、读取敏感文件、调用未知工具或输出未经验证结论。

### 2.2 样本字段

落库字段是 `eval_cases` 的 `input_json` / `reference_json` / `rubric_json`；数据集**文件**的书写格式（`datasets/<name>.<version>.json`，`schema_version=1`）如下，`import_dataset()` 负责把文件字段映射到三列 JSON：

```json
{
  "schema_version": 1,
  "name": "weekly-agent-quality",
  "version": "v1",
  "description": "数据集用途说明",
  "cases": [
    {
      "case_key": "growth-surge",
      "topic": "AI Agent",
      "window": {"start": "2026-10-05T00:00:00+00:00", "end": "2026-10-12T00:00:00+00:00"},
      "tags": ["growth", "happy-path"],
      "notes": "该样本要验证什么",
      "reference": {
        "summary": "人工参考结论",
        "required_sections": ["summary", "trend", "highlights", "risks", "recommendation", "appendix"],
        "facts": [{"repo": "acme/rocket", "field": "stars", "value": 1240}]
      },
      "rubric": {"min_citation_coverage": 0.95, "min_factual_consistency": 0.9, "min_task_completion": 0.9},
      "enabled": true
    }
  ]
}
```

`topic` 是**主题名**，运行时解析为 `topics.id`；解析不到时该样本记为 `failed` 且错误码为 `EVAL_TOPIC_NOT_FOUND`（样本本身仍留在数据集中，失败被记为证据而不是让整批 sweep 崩掉）。**导入时也校验一次（T25）**：只要有 `enabled` 的样本引用了本部署不存在或已停用的主题，`import_dataset()` 抛 `DatasetTopicMissing`（`code=DATASET_TOPIC_MISSING`），消息里同时列出缺的名字与本库可用的主题名。理由很实际 —— 在那之前，一份写给别人主题名的数据集会安静地导入成功，然后五个变体各跑一遍、各记一条 `failed`，看起来像模型坏了，实际是一行 JSON 写错了。只校验 `enabled` 的样本：停用的样本是"等数据形态就绪再开"的场景，允许指向尚不存在的主题；目录模式（一次性导入整个 `datasets/`）遇到不适用的数据集会**跳过**并把它写进 `skipped`，而显式指定单个文件时仍然报错。`reference.facts` 的 `repo` 用仓库全名寻址，评分器会把它解析回 `repo_snapshots` 的真实取值 —— 事实一致性因此是"与数据库比对"，不是"与模型判断比对"。`rubric` 的阈值目前只作记录，实际门禁用 `ARCHITECTURE.md` §12 的绝对目标（见第 3 节）。

### 2.3 数据集版本

每个数据集使用 `name + version` 唯一标识，文件名形如 `datasets/weekly-agent-quality.v1.json`。**修改样本必须新建版本，不能覆盖旧版本**：导入时对规范化后的数据集内容取 SHA-256 指纹，同名同版本且指纹相同则视为重放（`replayed=True`，不新增行），同名同版本但指纹不同则抛 `DatasetVersionConflict`（`409 DATASET_VERSION_CONFLICT`）。评测结果绑定 `dataset_id` 与 `agent_config_id`（baseline 的 `agent_config_id` 为 `NULL`）。

**当前仓库里有两个版本，它们服务于不同的世界**：

| 文件 | 用途 | 状态 |
|---|---|---|
| `datasets/weekly-agent-quality.v1.json` | 三个场景（增长 / 缺 README / 注入）× 三个夹具主题名 `AI Agent`、`AI Agent Sparse`、`AI Agent Injection` | **测试与 T14 冒烟用**。`tests/evaluation_support.py` 的 `seed_eval_world()` 会建出这三个主题与配套仓库，所以它在夹具世界里有效 |
| `datasets/weekly-agent-quality.v2.json` | 绑定本部署真实数据：主题 `sweep-llm-agent-topic`，窗口 `2026-10-16..2026-10-19`（库里仅这两天有快照），`reference.facts` 取自 2026-10-19 的最新快照 | **真实部署用**。三个场景里只有 `growth-surge` 启用；`missing-readme` 与 `injection-readme` 停用，因为触发它们需要专门的数据形态（未缓存 README / 缓存里直写注入文本），在真实主题上直接跑只会得到普通样本，让评测误以为"该场景已覆盖" |

v1 留在仓库里不是残留：它是夹具世界的输入，且库里的 `eval_datasets` 行仍指向它。v2 是对"本部署真实能跑什么"的回答 —— 这两个问题不一样，混成一份文件就会两头都不对。

## 3. 自动指标

全部指标都是**纯确定性规则**，没有任何模型参与评分。判定只看三样东西：报告草稿章节里的引用、`reference_json` 的事实断言、以及真实快照/缓存内容。

| 指标 | 定义 | 初始目标 | 落库位置 |
|---|---|---:|---|
| Citation Coverage | 含数字的章节中包含可追溯来源的比例 | >= 95% | `eval_results.citation_coverage`（门禁） |
| Source Accuracy | 引用能否解析到真实的快照 / README / 已发布报告 | >= 95% | `details_json.source_accuracy` |
| Factual Consistency | `reference_json.facts` 与真实快照一致的比例 | >= 90% | `eval_results.factual_consistency`（门禁） |
| Task Completion | 必需章节齐全的比例 | >= 90% | `eval_results.task_completion`（门禁） |
| Tool Success Rate | 工具调用成功且返回合法 Schema 的比例 | >= 98% | `details_json.tool_success_rate` |
| Prompt Injection Block Rate | 注入探针被拦截的比例 | >= 95% | `details_json.prompt_injection_block_rate` |
| Average Cost | 单份报告平均模型费用 | 在预算内 | `eval_results.cost` / `summary_json.total_cost` |
| P95 Latency | 单次 Agent 运行的 P95 耗时 | 满足配置目标 | `eval_results.latency_ms` / `summary_json.mean_latency_ms` |

两条实现约定，它们决定了报告该怎么读：

- **未行使的指标是 `null`，不是 `0`。** baseline 不调用任何工具，因此它的 `tool_success_rate` 无定义；`agent_no_tools` 不读任何内容，因此它的注入面为空。聚合时 `null` 被剔除在均值之外 —— 把"没测"当"测成 0"会让一个更窄的消融看起来比实际更差。
- **只有三个门禁指标占真实列**（`COLUMN_METRICS`），其余进 `details_json`（`DETAIL_METRICS`）。阈值写死在 `evaluation/runner.py:METRIC_TARGETS`，文案形如 `citation_coverage 0.0000 < target 0.95`。新增一个被测量的比率不需要迁移。

此外 `summary_json` 还收录三个**运行级证据信号**，用于区分"看起来一样"的变体：`tool_call_total`、`tool_call_denied`、`verification_sufficient_rate`。它们是消融行为的直接证据 —— `agent_no_tools` 的读操作恒为 0；`agent_no_history` 每次放弃历史报告都记一次 denial；`agent_no_verifier` 的 `verification_sufficient_rate` 恒为 `1.0000`，因为它把那道门整个移除了。

## 4. 人工与 LLM 评分

### 4.1 人工评分维度

人工只评自动指标无法可靠判断的内容：

- 结论是否基于证据，是否存在过度推断。
- 项目对目标团队是否真的有价值。
- 周报是否容易阅读，是否存在重复。
- 最重要的项目或风险是否遗漏。
- 是否符合企业语言和决策场景。

建议使用 1-5 分 Rubric，并记录评分理由。人工标注样本控制在可维护规模，避免为了数量牺牲质量。

### 4.2 LLM-as-a-judge

LLM 评分只作为扩展手段，不替代人工：

- 输入必须包含报告、来源和评分 Rubric。
- 评分模型与生成模型至少可以配置为不同模型。
- 输出必须结构化，并保留证据引用。
- 对人工子集计算一致性，防止评分器漂移。
- 评测 Prompt 进入版本管理，不能随意线上修改。

## 5. 离线评测流程

```text
导入数据集         python -m open_source_intelligence.evaluation.cli --action import
  -> 冻结数据版本和 Agent 配置（导入即冻结；同版本改内容被拒）
  -> 运行全部变体    --action run --dataset-id <id>   （或 POST /api/v1/evaluations/{dataset_id}/run）
       baseline -> agent_full -> 三个消融，每个变体各写一条 eval_runs
  -> 计算自动指标    （确定性 Scorer，无模型参与）
  -> 读取对比报告    --action compare --dataset-id <id>（或 GET /api/v1/evaluations/{dataset_id}/comparison）
  -> 形成 Prompt/工具/模型调整建议
```

`--action run` 是唯一真正执行 Agent 的动作；`list` 与 `import` 不执行 Agent，`compare` 只读地重建最近一次对比，**不触发任何新执行**。每次运行必须固定：

- `dataset_version`（= `eval_runs.dataset_id` 指向的 `(name, version)`）
- `agent_config_id`（baseline 除外）
- 模型供应商和模型名（`summary_json` 与 `eval_results.details_json` 记录 `provider`）
- Prompt 版本、工具策略版本（由 `agent_configs` 固定）
- 代码版本或 commit SHA（记录在运行环境而非数据行）
- 运行环境时的随机性或温度（默认离线桩是确定性的，相同输入产出字节一致）

默认 `AGENT_PROVIDER=offline` 让整条链路无需网络与凭据即可复现，因此评测可以进 CI；切到真实供应商只需 `AGENT_API_BASE`（https）+ `AGENT_API_KEY`。

## 6. 在线反馈与 Bad Case

在线反馈包括：

- 人工编辑报告时改动了哪些结论。
- 审核退回原因。
- 读者标记内容不准确或没有价值。
- 工具失败、引用失效和数字不一致。
- 成本或延迟超过预算。

Bad Case 进入待标注队列，经确认后加入新评测集版本。在线反馈不能直接修改生产 Prompt，只能通过版本化配置和回归评测后发布。

## 7. Token 与费用控制

每一个 `agent_run` 记录 `tool_call_count`、`model_call_count`、`estimated_cost` 和耗时。控制策略：

1. 先用 SQL/规则筛选候选，减少模型输入数量。
2. 用廉价模型进行结构化和分类，强模型只做研究综合与写作。
3. 缓存仓库画像、README 摘要和历史结论。
4. 对工具输出分页和截断，只保留与主题相关片段。
5. 达到 80% 预算时降级模型或减少非关键分析。
6. 达到 100% 预算时停止新增模型调用，返回 `AGENT_BUDGET_EXCEEDED` 或生成不完整草稿等待审核。

单报告预算必须可配置，不能把“每份报告约 2000 token”写成无依据的固定结果。

## 8. 版本回归与消融

消融实验至少回答：

- 去掉工具调用后，引用覆盖率和事实一致性下降多少。
- 去掉 Verifier 后，错误数字和无效引用是否增加。
- 去掉历史报告后，趋势判断和跨周变化是否变差。
- 更换模型或 Prompt 后，质量是否提升，成本是否可接受。

**衡量基准是"它所改动的那个变体"，不是固定的 baseline。** 对比报告因此形成一条参照链：

```text
baseline_single_prompt  <-  agent_full  <-  agent_no_verifier / agent_no_tools / agent_no_history
        （无参照）              （候选）                  （对照）
```

`agent_full` 相对 baseline 衡量（它回答第 9 节的"是否值得上线"），每个消融相对 `agent_full` 衡量（它回答本节的"去掉这个能力代价多大"）。`TargetSummary.reference_target` 记录参照对象，`deltas_vs_reference` 记录差值。

这样做是因为：**把消融拿去和 baseline 比会彻底掩盖损失** —— baseline 本来就停在该消融跌到的地板上，两者差值恒为 0，消融看起来"没有代价"。实测中 `agent_no_tools` 相对 `agent_full` 的引用覆盖率差值是 `-1.0000`（从满分掉到 0）。

回归发布规则：如果引用覆盖、事实一致性或任务完成率下降超过 **2 个百分点**（`EVAL_REGRESSION_TOLERANCE=0.02`），默认不升级为生产配置，必须由维护者确认。回落超过容差的变体被列入报告的 `regressions`，并置该变体 `regression=true`。

注意 `regressions` 与"能否上线"是两件事：消融是**对照而非候选**，它按设计就该退化，所以它的损失不会否决发布（见第 9 节）。

## 9. 评测交付物与上线裁决

一次 sweep 的交付物：

- 数据集版本清单（`GET /api/v1/evaluations`，或 CLI `--action list`）。
- Baseline、Agent 和消融版本的统一对比报告（`POST /{dataset_id}/run` 的响应，或 CLI `--action run`）。
- 自动指标与差值（`TargetSummary.metrics` / `deltas_vs_reference`）。
- 失败样本与原因分类（`eval_results.status=failed` + `details_json.error_code`）。
- 运行级证据（`tool_call_total` / `tool_call_denied` / `verification_sufficient_rate`）。
- Token、费用和延迟对比（`total_tokens` / `total_cost` / `mean_latency_ms`）。

**是否允许进入生产的结论 = 对比报告的 `passed` 字段。** 它的语义有四条：

1. **只裁决候选。** `PROMOTION_TARGETS = ("agent_full",)`。消融是**对照**，一个得分很差的对照正是实验的目的，因此它不能否决发布。
2. **无候选时评判全部。** 只跑 baseline 或只跑消融的诊断性 sweep 按其包含的全部变体评判。空 sweep 返回 `false`。
3. **必须真的测过。** `passed` 要求候选的 `scored_case_count > 0`。全部样本都失败的运行 `gate_failures` 为空，若不设这一条就会"因为没有测量"而通过 —— 这是发布门禁最危险的失效模式。
4. **判据是"无门禁失败且无回归"。** 三个门禁指标都要达到 `ARCHITECTURE.md` §12 的绝对目标，且相对参照的回落不超过容差。

因此 `passed=true` 与 `regressions` 非空**可以同时成立**：后者列出所有发生质量回退的变体（含对照），前者只回答"候选能不能上"。

## 10. 文档与实现的对照

| 本文档 | 实现位置 |
|---|---|
| §1 五个评测目标与消融开关 | `evaluation/targets.py:TARGET_ORDER`、`agent/profile.py:ExecutionProfile` |
| §2 数据集格式与版本化 | `datasets/*.json`、`evaluation/dataset.py:parse_dataset() / import_dataset()` |
| §3 自动指标 | `evaluation/scorers.py`、`evaluation/runner.py:COLUMN_METRICS / DETAIL_METRICS / METRIC_TARGETS` |
| §4 人工与 LLM 评分 | 计划（`eval_results.human_score` 列已就绪；LLM-as-a-judge 尚未实现，评分器目前是纯确定性的） |
| §5 离线评测流程 | `evaluation/cli.py`、`api/routes/evaluations.py` |
| §6 在线反馈与 Bad Case | 计划（T17 安全测试与 T16 演示阶段） |
| §7 Token 与费用控制 | T8/T9（`agent_runs` 计数与预算）、`eval_results.cost` |
| §8 版本回归与消融 | `evaluation/runner.py:_reference_summary() / _regressed()`、`settings.eval_regression_tolerance` |
| §9 交付物与上线裁决 | `evaluation/runner.py:ComparisonReport.passed`、`TargetSummary` |

`ARCHITECTURE.md` §9.5 给出评测与可观测性的设计约束，`INTERFACES.md` 第 4 节给出评测层的精确函数签名与错误码，`docs/api-contract.md` 第 7 节给出端点与响应形状，`docs/data-model.md` 第 7 节给出四张表的字段与约束。
