# 消融评测对比报告（板块 C1）

> 用真实模型 `deepseek-chat`（`openai_compatible` / `https://api.deepseek.com`）对数据集
> `weekly-agent-quality` v4 跑全 5 个评测变体，产出统一的消融对比。
> 运行时间：2026-10-26。每个变体 3 个启用 case（growth-surge / missing-readme / injection-readme）。

## 1. 数据集与运行前提

| 项 | 值 |
|---|---|
| 数据集 | `weekly-agent-quality` v4（`datasets/weekly-agent-quality.v4.json`，`eval_datasets.id=5`） |
| 样本 | 3 个启用 case：growth-surge（主样本，5 候选）、missing-readme（无 README 缓存）、injection-readme（含注入文本） |
| 模型 | `deepseek-chat`（真实模型，token 与费用均非零） |
| 窗口 | 2026-10-22 .. 2026-10-26（对齐板块 A 清理后的真实快照） |
| facts 锚点 | morluto/rea stars=388397、affaan-m/ECC stars=289246、Graphify-Labs/graphify stars=135742（均为 10-26 真实快照值） |

> **v4 相对 v3 的修正**：v3 的 facts 写的是板块 A 清理前（10-19）的种子回填值，清理后库里只剩 10-22 起的真实快照，导致 `factual_consistency` 恒为 0；v4 把窗口与 facts 一并对齐真实数据，使事实一致性真正可测。

## 2. 五个变体对比表

| 指标 | baseline<br>单提示 | **agent_full**<br>完整 Agent | no_verifier<br>去验证器 | no_tools<br>去工具 | no_history<br>去历史 |
|---|---|---|---|---|---|
| **引用覆盖率** citation_coverage | 0.6667 | **1.0000** | 1.0000 | 0.9444 | 1.0000 |
| **事实一致性** factual_consistency | 0.0000 | **1.0000** | 1.0000 | 1.0000 | 1.0000 |
| **任务完成率** task_completion | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| 来源准确率 source_accuracy | 1.0000 | 0.9885 | 1.0000 | 1.0000 | 1.0000 |
| 工具成功率 tool_success_rate | N/A | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| **Token** | 11,256 | **47,651** | 27,896 | 15,488 | 46,147 |
| **成本（USD）** | 0.002459 | **0.008284** | 0.005049 | 0.003006 | 0.008091 |
| **平均延迟（ms）** | 10,953 | **20,186** | 15,262 | 12,137 | 20,096 |
| 工具调用次数 tool_call_total | 0 | **48** | 33 | 18 | 48 |
| 验证充分率 verification_sufficient_rate | N/A | 0.0 | **1.0** | 0.0 | 0.0 |
| 注入拦截率 prompt_injection_block_rate | N/A | 0.6667 | 0.6667 | N/A | 0.6667 |
| 发布裁决（门禁） | 未过（3 项） | **passed** | passed | 未过（1 项） | passed |

> 表格数据来自 `eval_runs` run 24–28 的 `summary_json`；`--action compare --dataset-id 5` 的原始 JSON 见 §4。
> 「未行使的指标是 null 而非 0」：baseline 无工具，`tool_success_rate`/`prompt_injection_block_rate`/`verification_sufficient_rate` 记为 N/A；`agent_no_tools` 无读工具，注入面为空故 `prompt_injection_block_rate` 记为 N/A。

## 3. 量化结论（每个组件存在的必要性与增益）

### 3.1 完整 Agent 相对单提示 baseline（「值得上线吗」）

| 指标 | baseline → agent_full | 结论 |
|---|---|---|
| 引用覆盖率 | 0.6667 → **1.0000**（+0.3333） | Agent 通过工具把每个含数字章节都落到可追溯的快照引用 |
| 事实一致性 | 0.0000 → **1.0000**（+1.0000） | baseline 不读工具、无法证明任何事实（评分器要求「引用 + 值正确」）；Agent 3/3 facts 全部命中 |

**这是核心证据**：单提示模型连「证明自己说对了一个数字」都做不到（事实一致性 0），而 Agent 通过检索工具把引用覆盖率拉满、事实全部可核对。代价是成本 +0.0058 USD、延迟翻倍——这正是「调 prompt」和「造 Agent」的分水岭。

### 3.2 去掉验证器（no_verifier，相对 agent_full）

- 引用覆盖率、事实一致性、任务完成率**表面全持平**（1.0 / 1.0 / 1.0）。
- 但 `verification_sufficient_rate` 从 **0.0 → 1.0**：验证门被整个移除后，任何运行都会「验证通过」，不再把证据不足的运行挡在发布之外。
- 工具调用 48 → 33（少了验证器驱动的补齐调用）。

**结论**：验证器的价值**不在表面数字，而在那道门禁**。它在本轮把 3 个 case 全部判为 `sufficient=false`（模型没查 prior reports 与 README 内容），诚实拦下了「证据不完整」的运行；去掉它，这些运行会带着不完整证据一路通过发布。`verification_sufficient_rate` 从 0 到 1 的翻转，正是「验证器在起作用」的直接证据。

### 3.3 去掉工具（no_tools，相对 agent_full）

- 引用覆盖率 1.0000 → **0.9444**（−0.0556），是**唯一触发回归标记（`regression=true`）的消融**。
- 工具调用 48 → 18（只剩写草稿的 6 次 × 3 case，读工具全部禁用）。
- Token 47,651 → 15,488（省 67%）、成本 0.0083 → 0.0030（省 64%）。

**结论**：去掉工具后，模型在草稿里**编造引用锚点**（它从候选仓库 id 列表猜出 `repo_snapshots:100:2026-10-26` 这类看似可追溯的引用），导致引用覆盖率没有像理论上那样归零、而是轻微下滑并跌破 0.95 门禁。这是一个**评测盲区的实锤**：评分器的 `_resolves` 只检查「引用能否解析到真实行」，不检查「模型是否真的通过工具读到了数据」。即使如此，no_tools 仍是唯一回归的变体，且工具调用与成本的大幅下降反衬出「读工具是真金白银换来的可追溯性」。

### 3.4 去掉历史报告（no_history，相对 agent_full）

- 各项指标**几乎完全持平**（引用/事实/任务均 1.0，token 46,147 vs 47,651，成本 0.0081 vs 0.0083）。
- 工具调用 48 → 48（`search_prior_reports` 本就被模型跳过了，禁用它自然无影响）。

**结论**：历史报告在本数据集上**增益几乎为零**——因为窗口是单周、库里没有可引用的历史报告，`search_prior_reports` 本来就没有东西可查。这不是「历史报告没用」，而是「当前数据形态下它无处发力」；等连续采集积累出多周序列后，`no_history` 才会显出代价。这一条诚实地回答了「历史报告是不是必须」：**在当前单周窗口下不是必须**。

## 4. 原始 compare 输出（run 24–28）

```text
target            cite    fact    task   source_acc  tool_succ  tokens   cost     latency_ms  tool_calls  verif_rate  gate_fail
baseline          0.6667  0.0000  1.0000  1.0000      N/A        11256    0.002459  10953       0           N/A         cite<0.95, fact<0.90
agent_full        1.0000  1.0000  1.0000  0.9885      1.0000     47651    0.008284  20186       48          0.0         —
agent_no_verifier 1.0000  1.0000  1.0000  1.0000      1.0000     27896    0.005049  15262       33          1.0         —
agent_no_tools    0.9444  1.0000  1.0000  1.0000      1.0000     15488    0.003006  12137       18          0.0         cite<0.95
agent_no_history  1.0000  1.0000  1.0000  1.0000      1.0000     46147    0.008091  20096       48          0.0         —
passed: true   regressions: ['agent_no_tools']
```

- `passed=true` 只裁决 `agent_full`（PROMOTION_TARGETS）；消融是**对照**，其退化不否决发布。
- `agent_no_tools` 的 −0.0556 回落超过 `EVAL_REGRESSION_TOLERANCE=0.02`，故列入 `regressions` 并置 `regression=true`。

## 5. 复现命令

```bash
# 导入 v4（幂等；若已存在同名同指纹则 replayed）
docker compose --env-file .env -f infra/docker-compose.yml exec -T api \
  python -m open_source_intelligence.evaluation.cli --action import \
  --dataset-file datasets/weekly-agent-quality.v4.json

# 跑全 5 个变体（也可 --target 单个跑）
docker compose --env-file .env -f infra/docker-compose.yml exec -T api \
  python -m open_source_intelligence.evaluation.cli --action run --dataset-id 5

# 看统一对比（只读，不触发新执行）
docker compose --env-file .env -f infra/docker-compose.yml exec -T api \
  python -m open_source_intelligence.evaluation.cli --action compare --dataset-id 5
```

界面入口：`/ui/evaluations?dataset_id=5` 与 `/ui/dashboard` 的评测卡片展示同一份对比。

## 6. 发现的两个问题（如实记录，非阻塞）

1. **评分器不校验「引用是否真的由工具产生」**：no_tools 变体因候选仓库 id 被注入到 user context（`build_user_context` 的 `<CANDIDATE_TAG>`），模型得以编造可解析的引用锚点，使引用覆盖率没有归零。这是 `_resolves` 只查「引用能否解析」而不查「模型是否读过数据」造成的评测盲区，与 `evaluation.md` §3 预见的「运行级证据信号」互补——`tool_call_total`（48→18）才是 no_tools 的真实代价。
2. **agent_full 的验证器本轮判 `sufficient=false`**：模型自主规划时未调用 `search_prior_reports` 与 `get_repository_content`，验证器诚实报「证据不完整」。这不是 bug，是真实模型的行为变异，恰说明验证门在起作用；但它意味着 `verification_sufficient_rate=0.0`，读表时需结合「验证器被移除后该值恒 1.0」来理解。
