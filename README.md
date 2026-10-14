# 企业开源技术情报 Agent 与自动周报平台

> 项目状态：T1-T12 已完成，进入 T13
> 文档版本：v0.11
> 最后更新：2026-10-13

## 最终版路径

本项目所有代码、文档、配置和测试的最终版本统一存放于：

```text
D:\job-workspace\project_sources\open-source-intelligence-agent
```

其他位置即使存在副本，也只作为临时工作副本，不作为最终交付或验收依据。

## 项目定位

面向研发战略、架构和产品团队，自动采集公开开源项目动态，使用专用研究 Agent 完成趋势分析、证据补齐、质量验证和周报生成，并通过风险策略决定自动发布或进入人工审核。

本项目是老项目 `learning-planning-multi-agent` 的互补项目：

| 项目 | 主要证明能力 |
|---|---|
| 多智能体学习规划助手 | LangGraph、多 Agent、RAG、交互式 AI 应用 |
| 本项目 | 专用 Agent 工具调用、自动化 Pipeline、MySQL、工程化、评测与监控 |

## 文档索引

| 文档 | 用途 |
|---|---|
| [REQUIREMENTS.md](REQUIREMENTS.md) | 根需求基线、范围与验收标准 |
| [INTERFACES.md](INTERFACES.md) | 根接口基线，汇总 API、Tool 和跨模块对象 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 唯一真理源，约束架构、接口、数据和实施计划 |
| [docs/requirements.md](docs/requirements.md) | 详细产品需求、角色、功能与验收标准 |
| [docs/api-contract.md](docs/api-contract.md) | REST API、请求响应对象和错误约定 |
| [docs/agent-tool-contract.md](docs/agent-tool-contract.md) | Agent 内部工具、权限、错误码和测试契约 |
| [docs/data-model.md](docs/data-model.md) | MySQL 表结构、关系和运行时不变量 |
| [docs/security-and-privacy.md](docs/security-and-privacy.md) | 权限、数据保护、模型调用和审计边界 |
| [docs/evaluation.md](docs/evaluation.md) | Agent、报告质量和成本评测方案 |
| [docs/observability.md](docs/observability.md) | 监控指标口径、窗口语义、运行账本与看板 |
| [docs/backfill.md](docs/backfill.md) | 历史回填：分页语义、限流语义、断点续传与并发守卫 |
| [docs/development-process.md](docs/development-process.md) | 企业流程的个人开发精简执行版本 |
| [docs/technology-learning-checklist.md](docs/technology-learning-checklist.md) | 按实施任务排列的技术学习清单 |
| [docs/readiness-and-startup-roadmap.md](docs/readiness-and-startup-roadmap.md) | 准备度检查和正式开工路线图 |
| [docs/adr](docs/adr) | 关键架构决策记录 |

## 当前阶段

当前 T16 已完成：历史数据回填与端到端演示已跑通。回填是一条**可中断、可续传**的走查路径，向一个源按页连着提问，把历史仓库落进目录表——`backfill_jobs` 的作业行就是检查点（游标、已收条目、尝试次数、心跳都在列里），每页提交一次且**当页仓库与游标同一事务**，所以「游标动了」必然意味着「有一页落地了」；中断后 `resume` 从游标继续，只请求尚未走过的页。它只有 CLI 与 Celery 两条触发路径（`--action start|resume|status|list`，一次只走一个 provider），**不暴露 HTTP 端点、也不进 Beat**：回填是一次性、昂贵、对共享公共 API 施压的动作，触发权属于操作员。

分页的边界由源自己说，不由条目数推断——GitHub 用响应头 `Link` 里的 `rel="next"`（走到 1000 条搜索上限后它仍报完整 `total_count` 却不再宣告 `next`），Hugging Face 镜像用 `skip`/`limit` 偏移并以「返回不足一页」为终止信号。限流保证的是**请求起始间隔**（`60/per_minute`，第一次免费），不是固定睡眠，也不是允许突发的令牌桶；默认预算取自数据源行上的 `rate_limit_per_minute`。协议见 [docs/backfill.md](docs/backfill.md)。

端到端演示把回填喂出的库交给既有链路跑通：`run_daily_pipeline`（collect → snapshot → rank）→ Research Agent → `generate_report` → `verify_report` → 发布策略判定 → 人工 `approve` → `publish_due_reports` 发布，落库行再由**只读会话**重新读回校验。

模型接入采用可插拔 Adapter：默认 `AGENT_PROVIDER=offline` 使用确定性离线桩，无需网络与 API Key 即可端到端验证；`OpenAICompatibleClient` 通过同一个 `ModelClient` 接口对接真实供应商。

`ARCHITECTURE.md` §14 中尚余 **T17 安全测试与故障演练**、**T18 文档同步与发布检查** 未开始。

T1 验证命令：

```powershell
.\scripts\verify_t1.ps1
```

该脚本会强制重建镜像，并执行 pytest、Ruff、mypy、Compose、API、Worker 和 Beat 验证。

T2 验证命令：

```powershell
.\scripts\verify_t2.ps1
```

该脚本使用一次性 MySQL 数据库验证迁移升级、回滚、再次升级和模型一致性，不修改正式开发库。

T3 验证命令：

```powershell
.\scripts\verify_t3.ps1
```

该脚本会重建服务、应用迁移、引导临时管理员、验证登录和当前用户、检查 401，并确认审计记录已持久化。

T4 验证命令：

```powershell
.\scripts\verify_t4.ps1
```

该脚本执行真实双源采集，并重复采集一次确认仓库数量不变。

T5 验证命令：

```powershell
.\scripts\verify_t5.ps1
```

该脚本在现有真实仓库上执行 3 次快照、2 次候选排序，验证幂等、窗口增量、排序和审计后清理验证数据。

T6 验证命令：

```powershell
.\scripts\verify_t6.ps1
```

该脚本执行每日 Pipeline 的 MySQL 冒烟（运行记录、attempt 轨迹、错误脱敏、幂等和 CHECK 约束），通过真实 Celery broker 和 Worker 调度任务，用一个专用 Worker 指向不可达数据源以证明失败重试留下两条尝试记录，验证 Beat 定时触发与 CLI 状态查询，最后清理验证数据。

T7 验证命令：

```powershell
.\scripts\verify_t7.ps1
```

该脚本执行 pytest、Ruff、mypy，重建服务并检查迁移漂移，然后在真实 MySQL 上调用全部 5 个白名单工具，验证 Schema 严格校验、跨主题 ACL 拒绝、内容清理与截断、不可信标记、草稿幂等与版本递增、只读工具零写入以及 `tool_calls` 记录，最后清理验证数据。

T8 验证命令：

```powershell
.\scripts\verify_t8.ps1
```

该脚本执行 pytest、Ruff、mypy，重建服务并检查迁移漂移，然后在真实 MySQL 上跑通完整研究轨迹：断言 17 个步骤的 `step_index` 连续且类型覆盖 `plan/tool/observe/verify/write`、13 次工具调用全部成功且绑定同一 `agent_run`、6 个草稿章节版本为 1 且带引用、只读工具零写入，并验证「无候选」（`needs_review`）与「模型不可用」（`failed`）两条降级路径，最后清理验证数据。

T9 验证命令：

```powershell
.\scripts\verify_t9.ps1
```

该脚本执行 pytest、Ruff、mypy、迁移和 drift 检查，然后在真实 MySQL 上验证正常 Token 持久化、80% 降级仍能写草稿、100% 预算在第二次模型调用前停止，以及恶意 README 被阻断、标记 `AGENT_PROMPT_INJECTION` 且不产生越权工具调用。

T10 验证命令：

```powershell
.\scripts\verify_t10.ps1
```

该脚本在真实 MySQL 上先跑通 Agent 草稿，再验证六章节报告合成、16 条可追溯引用、metric 快照绑定、重复生成幂等、内容变化版本递增，以及伪造引用在写入前被拒绝。

T11 验证命令：

```powershell
.\scripts\verify_t11.ps1
```

该脚本执行 pytest、Ruff、mypy、五服务重建、迁移升级/回滚/再次升级与 drift 检查，然后在真实 MySQL 上验证三条验收：伪造引用被 Verifier 拦截、`AUTO_WITH_DELAY` 报告在窗口前不发布且窗口后自动发布、`MANUAL` 报告缺少审核记录时无法发布。

T12 验证命令：

```powershell
.\scripts\verify_t12.ps1
```

该脚本执行 pytest、Ruff、mypy、五服务重建、迁移升级/回滚/再次升级与 drift 检查，然后在真实 MySQL 上启动**真实 FastAPI 应用**跑通 T12 验收：报告列表按可见范围在数据库层过滤（`VIEWER` 看不到 `PRIVATE`，`ADMIN` 全见）、详情页与 API 的越权读取返回 `403` 且不泄露标题、CSRF 缺失/不匹配与只读角色写操作均返回 `403`、编辑正文追加不可变版本并回到 `draft`、`MANUAL` 走完「转人工 → 退回修改 → 再编辑 → 通过 → 发布」、已发布报告可回退为新的 `draft` 版本，并检查六类审计事件与验证/审核证据落库。

T13 验证命令：

```powershell
.\scripts\verify_t13.ps1
```

该脚本在真实 MySQL 上驱动出站推送链路：只有 `approved` / `published` 报告可被推送、`CONFIDENTIAL` 额外要求同版本 `approve` 审核、允许列表的 `enabled` 与渠道一致以及默认关闭的 `allow_private` / `allow_confidential` 共同把关；**传输失败不是异常**（适配器返回 `ok=False` → 落库 `failed` + `error_code`，接口仍返回 `200`），只有守卫违规才抛错；幂等键让重复请求不再投递（`replayed=True`），失败重试推进同一行的 `attempt`。

T14 验证命令：

```powershell
.\scripts\verify_t14.ps1
```

该脚本执行评测链路的真实运行：导入版本化数据集（同内容重放不新增行、同版本异内容返回 `409`）、按 `baseline_single_prompt` / `agent_full` / 消融变体分别执行并落真实测量，再重建统一对比报告——`passed` 只裁决 `agent_full`，消融只作对照，且 `scored_case_count > 0` 是通过的必要条件。

T15 验证命令：

```powershell
.\scripts\verify_t15.ps1
```

该脚本在真实 MySQL 上校验只读监控层：窗口内任务成功率、Token、成本、延迟分位数（nearest-rank，不插值）、失败原因分布、工具成功率与报告人工阻断率全部真实可见，**未行使的指标返回 `null` 而不是 `0`**；评测段固定展示最新一轮 sweep 且 `passed` 直接读 `compare_latest` 不重算；`/ui/dashboard` 与运行轨迹页在真实应用上渲染，`VIEWER` 被拒且导航栏不暴露入口。

T16 验证命令：

```powershell
.\scripts\verify_t16.ps1
```

该脚本执行历史回填与端到端演示：在一个 GitHub 形状的 mock transport 上跑真实 `BackfillJob` 走查（分页边界、第 3 页注入 `500` 后中断并只续传第 3、4 页、重放幂等、页预算与源限流预算、并发守卫与过期接管、越界与不可分页源被拒），随后把回填喂出的库交给真实 `run_daily_pipeline` → Agent → 报告生成 → 验证 → 发布策略 → 人工 `approve` → `publish_due_reports` 完整跑通，并由只读会话重新读回校验，同时驱动真实 CLI 子进程的 `list` / `status` 与两处拒绝路径。

## 开发原则

1. 数据采集和指标计算保持确定性，Agent 负责研究决策、证据补齐、验证和综合写作。
2. 常规低风险周报采用 `AUTO_WITH_DELAY`，不为追求安全而默认阻塞人工审核。
3. 企业权限、审计、模型数据保护和 Token 成本以轻量方式实现，保留扩展接口，不宣称完成完整 DLP、ABAC 或多租户。
4. 每次代码变更若影响架构、接口、数据模型、依赖或目录结构，必须同步回写 `ARCHITECTURE.md`。
