# 可观测性协议（T15）

> 版本：v1
> 最后更新：2026-10-14
> 实现：`src/open_source_intelligence/observability/`（聚合 `metrics.py` / 读侧 `queries.py` / 错误 `errors.py`）
> 接口形状：`docs/api-contract.md` §5、§8

## 1. 目标与验收

T15 要回答三个运维问题，`ARCHITECTURE.md` §14 把第一个写成了验收条件：

1. 任务、Token、延迟、失败率和评测结果**可见**。
2. `ARCHITECTURE.md` §12 的"常规报告人工阻塞率"有真实数字。
3. 能从一份失败的报告**追到具体的那次工具调用**（`ARCHITECTURE.md` §14 的验收条件）。

三条都由只读路径满足：监控层不写任何一行，也不新增任何一张表。

## 2. 数据来源

| 表 | 贡献的指标 |
|---|---|
| `pipeline_runs` | 任务总数、状态分布、按 `run_type` 的分布、成功率、时长分位数 |
| `agent_runs` | 运行总数、状态分布、发布模式分布、Token、成本、成功率、失败原因、时长分位数 |
| `tool_calls` | 工具调用总数、状态分布、成功率、每个工具的分布与时长、被拒次数 |
| `reports` | 报告总数、状态分布、人工阻断率 |
| `eval_runs` / `eval_results` | 最新一轮 sweep 的变体、指标、门禁失败与裁决（**经评测层转发，不重算**） |
| `agent_steps` | 轨迹：步骤顺序、每步耗时与摘要、以及每个步骤下的工具调用 |

## 3. 时间窗口

- 窗口是**半开区间**：`start <= started_at < end`，`end` 取请求时刻。边界所在的那一秒只算一次，两次相邻查询不会重复计入同一条运行。
- `tool_calls` 没有 `started_at`，按 `created_at` 落入窗口。
- `window_days` 默认 `7`，允许 `1..90`。
- **端点上越界是拒绝（`422`），不是夹取。** 夹取会返回一个与请求不符的答案，而两边的数字都"看起来正常"，偏差永远不会被发现。
- `resolve_window()` 仍然夹取。它服务于绕过端点直接调用聚合层的代码，让它们无法请求一个账本本不打算被这样读的窗口。两处行为不同是刻意的，改动任一处都要同时确认另一处。
- **评测段不受窗口影响**，见 §5。

## 4. 指标定义

比率一律四位小数、`ROUND_HALF_UP`，以字符串出站（`"0.6250"` = 62.5%）。

| 指标 | 口径 |
|---|---|
| `pipeline.success_rate` | `succeeded / (succeeded + failed + partial)`；`queued` / `running` 计入 `total` 但不进分母（它们还没有结果） |
| `agent.success_rate` | `(completed + needs_review) / (completed + needs_review + failed)`。**`needs_review` 算成功**：它产出了报告，只是停下来等人；`failed` 是唯一的落败 |
| `agent.failure_reasons` | 窗口内 `agent_runs.error_code` 的分布（只统计非空值） |
| `agent.tokens_per_run` | `total_tokens / 运行数`，四位小数 |
| `agent.estimated_cost` | 窗口内 `estimated_cost` 求和。可加量，无样本时为 `0` |
| `tools.success_rate` | `success / (success + failed + denied)`。**`denied` 计入失败**：把被拒的越权尝试排除掉，会让一个反复越权的 Agent 在成功率上看起来更健康 |
| `tools.by_tool` | 每个工具独立的调用量与时长，按调用量降序 |
| `reports.manual_block_rate` | 状态为 `pending_review` 的报告 / 窗口内全部报告。这就是 §12 的"常规报告人工阻塞率"，与 T11 的 `changes_requested` 分开定义 |
| `latency.p50_ms` / `p95_ms` | 见 §5 的分位数定义；`samples` 是支撑该分位数的观测数 |

所有口径常量只定义在 `observability/metrics.py`：`PIPELINE_OUTCOME_STATUSES`、`PIPELINE_SUCCEEDED_STATUSES`、`AGENT_SUCCEEDED_STATUSES`、`AGENT_OUTCOME_STATUSES`、`TOOL_FAILED_STATUSES`、`TOOL_SUCCEEDED_STATUSES`、`MANUAL_BLOCK_STATUS`。改口径只改这一处。

## 5. 三条语义约定

**① `null` 不是 `0`。** 分母为零时比率是 `null`；`samples == 0` 时 `p50_ms` / `p95_ms` 也是 `null`。空窗口必须与"全是真实零"可区分：前者是"没测量"，后者是"测到了零"。只有 `estimated_cost` 这类可加量在无样本时为 `0`。落到界面上是 `—` 而不是 `0.0%`，落到 JSON 里是 `null`。

**② 分位数是 nearest-rank，不插值。** `p95_ms` 取排序后第 `ceil(0.95 * n)` 个样本，因此它一定是**某一次运行真实经历过的时长**。运维读 P95 想知道"最慢的那次有多慢"，插值给出的是一个从未发生过的数。

**③ 评测段不做窗口过滤，也不重算裁决。** 评测是运维触发的低频动作，1 天窗口通常什么也没有，因此这一段固定展示**最新一轮 sweep**。`passed` 直接来自 `evaluation.runner.compare_latest`，监控层只做转发：看板上的绿灯因此不可能与它摘要的那份评测报告打架，将来裁决规则变化时两者也不会漂移。从未跑过评测时 `dataset` 为 `null`、`targets` 为空，而不是报错 —— 没有评测不是故障。

## 6. 权限

`METRICS_ROLES = (ROLE_ADMIN, ROLE_EDITOR)`。总览与运行账本暴露跨主题的成本、失败原因与评测裁决，属于运维面，因此与报告读者分开：`VIEWER` 能读报告，但不读产生报告的那本账。`VIEWER` 得到 `403`，匿名得到 `401`，两者都不透露目标运行是否存在。

界面遵守同一条规则，并且**导航栏只在有权访问时渲染「运维看板」入口** —— 无权用户看不到通向 `403` 的死链。

## 7. 端点与页面

| 入口 | 作用 |
|---|---|
| `GET /api/v1/metrics/overview?window_days=` | 运维总览：窗口 + 五个只读区块 |
| `GET /api/v1/agent/runs` | 运行账本，最新在前；支持 `status` / `topic_id` 过滤与分页（`page_size` 上限 100） |
| `GET /api/v1/agent/runs/{run_id}` | 单次运行的预算消耗与结果 |
| `GET /api/v1/agent/runs/{run_id}/steps` | 轨迹：有序步骤 + 每步的工具调用 |
| `GET /ui/dashboard?window_days=` | 看板页（六个区块，1 / 7 / 30 天切换） |
| `GET /ui/runs/{run_id}` | 运行详情页：错误码人话、有无产出报告、复核结论与表单、步骤轨迹 |
| `POST /ui/runs/{run_id}/review` | 给 `needs_review` 运行追加复核结论（`agent.review`），不改运行状态 |
| `GET /ui/dashboard/runs/{run_id}` | **已废弃**，`303` 到 `/ui/runs/{run_id}` |

运行详情挂在 `/ui/runs/{run_id}`：它属于列出它的那张台账，挂 `dashboard` 下时从「采集与运行」点进去会看到侧栏高亮「运维看板」。

**Agent 运行对 API 是只读的。** HTTP 只提供上面三个 `GET`：一次运行会消耗模型预算，触发权属于操作员与调度器，而不是任意一个已登录客户端。**运行有两个来源**：操作员 CLI（`agent/cli.py --action run`，或 `pipeline.cli --action daily --with-research`），以及每日任务在 `AGENT_AUTO_RESEARCH_ENABLED=true` 时的派发（T24，见 `pipeline/research_dispatch.py`；默认关闭）。`workers.tasks.run_research_agent` 仍**不单独进 beat 表** —— 两个调度源会让同一次研究双倍花钱。

**`needs_review` 是终态，但复核有落点。** 这类运行跑完了却没有产出报告（最常见是 `AGENT_EVIDENCE_INCOMPLETE`：缺 README 或缺可比较的历史报告），所以没有"通过"这个动作可做；界面提供的是把**读过的判断**记下来（`agent_run_reviews`，只增不改），并在台账上显示"谁看过、结论是什么"，`?review=open` 就是还没人看过的那批。

路由声明顺序是结构性约束：`/api/v1/agent/runs` 必须先于 `/api/v1/agent/runs/{run_id}`、`/ui/runs` 必须先于 `/ui/runs/{run_id}`，否则字面量段会被路径参数吞掉 —— 与 `/reports/publish-due`、评测 `/runs` 是同一类陷阱。

## 8. 出站脱敏

UI 与 JSON API **都是 egress 路径**，因此都过 `core/logging.py:redact_text`。轨迹里的 `summary`、`result_summary` 以及与 `arguments` 同形状的 JSON 文本在离开进程前一律遮蔽；服务端渲染侧另有 `router.py` 的两个 Jinja 过滤器（`redact` 单值、`redacted_json` 整个 JSON 列）。一条规则、两处实现位置，不允许只在其中一条路径上生效。

## 9. 轨迹与"未挂到步骤上的工具调用"

`AgentRunTrace.unlinked_tool_calls` 保留 `agent_step_id` 为 `null` 的工具调用，而不是丢掉。一次在步骤落库前就被预算硬停的运行，其失败证据恰在这些行里；丢弃它们会让"从失败报告追到具体工具调用"重新落空。轨迹页为它们单独留了一张卡片。

## 10. 文档与实现对照

| 概念 | 实现位置 |
|---|---|
| 窗口、分位数、比率、状态口径 | `observability/metrics.py` |
| 取数、分页、窗口解析、轨迹组装 | `observability/queries.py` |
| `NOT_FOUND` 到 `404` 的映射 | `api/errors.py:_domain_status` + `ObservabilityError` 处理器 |
| 角色守卫 | `security/authorization.py:METRICS_ROLES` |
| 端点契约 | `docs/api-contract.md` §5、§8 |
| 跨阶段接口基线 | `INTERFACES.md` §3、§4、§5 |
| 架构说明 | `ARCHITECTURE.md` §9.12 |
