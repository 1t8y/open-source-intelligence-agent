# Agent、报告与成本评测方案

> 目标：证明专用 Agent 相比单 Prompt 的增益，并持续监控质量、风险和成本。  
> 最后更新：2026-09-24

## 1. 评测对象

| 版本 | 说明 |
|---|---|
| `baseline_single_prompt` | 把候选数据拼接后一次性交给模型生成报告 |
| `agent_full` | Research、工具调用、Verify、Write 完整流程 |
| `agent_no_verifier` | 去掉 Verifier，用于消融 |
| `agent_no_tools` | 只使用一次候选数据，不调用历史趋势和 README 工具 |
| `agent_no_history` | 保留工具，但不读取历史报告 |

评测不是为了追求学术指标，而是回答三个问题：报告是否可信、是否节省人工、Token 是否可控。

## 2. 数据集设计

### 2.1 样本来源

- 历史周报和人工核对过的项目分析。
- 公开仓库在不同 star 增速、语言和技术方向上的组合。
- 人工构造的边界样本：数据缺失、README 噪声、来源冲突、重复项目和异常增长。
- Prompt Injection 样本：要求 Agent 忽略规则、读取敏感文件、调用未知工具或输出未经验证结论。

### 2.2 样本字段

```text
EvalCase {
  case_key: string
  topic: object
  window: { start: datetime, end: datetime }
  input_snapshot: object
  reference_summary: string | null
  reference_facts: list<FactRef>
  rubric: object
  tags: list<string>
  enabled: boolean
}
```

### 2.3 数据集版本

每个数据集使用 `name + version` 唯一标识。修改样本后新建版本，不能覆盖旧版本。评测结果必须绑定 `dataset_id` 和 `agent_config_id`。

## 3. 自动指标

| 指标 | 定义 | 初始目标 |
|---|---|---:|
| Citation Coverage | 关键数字和结论中包含可追溯来源的比例 | >= 95% |
| Source Accuracy | 引用内容真实支持对应结论的比例 | >= 95% |
| Factual Consistency | 报告陈述与数据库/来源一致的比例 | >= 90% |
| Task Completion | 完成指定章节和项目分析数量的比例 | >= 90% |
| Tool Success Rate | 工具调用成功且返回合法 Schema 的比例 | >= 98% |
| Prompt Injection Block Rate | 恶意输入未触发越权行为的比例 | >= 95% |
| Average Cost | 单份报告平均模型费用 | 在预算内 |
| P95 Latency | 单次 Agent 运行的 P95 耗时 | 满足配置目标 |

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
准备数据集
  -> 冻结数据版本和 Agent 配置
  -> 运行 baseline
  -> 运行 agent_full
  -> 运行消融版本
  -> 计算自动指标
  -> 人工抽样复核
  -> 生成差异报告
  -> 形成 Prompt/工具/模型调整建议
```

每次运行必须固定：

- `dataset_version`
- `agent_config_id`
- 模型供应商和模型名
- Prompt 版本
- 工具策略版本
- 代码版本或 commit SHA
- 运行环境时的随机性或温度

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

回归发布规则：如果引用覆盖、事实一致性或任务完成率下降超过 2 个百分点，默认不升级为生产配置，必须由维护者确认。

## 9. 评测交付物

- 数据集版本清单。
- Baseline、Agent 和消融版本结果。
- 自动指标和人工评分结果。
- 失败样本和原因分类。
- Token、费用和延迟对比。
- 是否允许进入生产的结论。
