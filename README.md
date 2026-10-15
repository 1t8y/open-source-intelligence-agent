# 企业开源技术情报 Agent 与自动周报平台

> 项目状态：T1-T18 已完成（实施任务清单见 `ARCHITECTURE.md` §14）
> 文档版本：v0.12
> 最后更新：2026-10-15

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

当前 T18 已完成：文档同步与发布检查已跑通，`ARCHITECTURE.md` §14 的实施任务清单至此**全部完成**。

T18 的交付物是一条命令 —— `scripts/check_docs_drift.py`，十项比较，任何一项不通过就退出非零：

```text
 1 endpoints             契约声明的每个端点都有已注册路由；每条已注册路由都在契约里被提到
 2 tables                Base.metadata 的每张表都在 docs/data-model.md 有小节；文档里有而代码里没有的必须自带"计划内"标注
 3 architecture-summary  ARCHITECTURE.md §7 的摘要表列全 Base.metadata 的每张表，且计划表必须带标注
 4 evaluation-targets    docs/evaluation.md §1 的变体表与 TARGET_ORDER 逐字同序，且点名 PROMOTION_TARGETS
 5 configuration         Settings 的每个字段都在 .env.example 里；反之 .env.example 不能多出未登记的非 Compose 键
 6 doc-index             docs/ 的每个文件都被 README 索引到；README 的每个 docs/ 链接都指向存在的文件
 7 migrations            只有一个 Alembic head
 8 secrets               被 git 跟踪的 .env.example 里没有凭据形状的值
 9 status-lines          README/INTERFACES 的状态行点名 §14 里最后完成的任务
10 version               __version__ 与 pyproject.toml 一致
```

**为什么是"比较"而不是"读一遍"。** 第一次按 T18 的字面要求去读文档时，查出了四组从未实现的接口：`/sources`、`/topics`（含 `POST /topics/{id}/run`）、`/repositories`（含 snapshots）、`/audit-logs` —— `INTERFACES.md` §3 与 `docs/api-contract.md` 声明了它们，代码里一条都没有。**读**正是让这件事漂移了十七个任务都没被发现的原因：读者看到的是一张看着合理的端点表，而不是一份 diff。同一个读法也只查出了两处（`feedback` 被当成已实现的表、`.env.example` 缺 `APP_NAME`），写成比较之后又揪出两处（`ARCHITECTURE.md` §7 摘要漏 5 张表、`README.md` 的状态行停在 T13）。

**处置分两类。** 一是**实现**：`REQUIREMENTS.md` FR-D-05 点名要求「报告、**仓库**、Agent、评测和**审计** API」，所以 `/repositories`（含 `/repositories/{id}/snapshots`，闭区间窗口）与 `/audit-logs` 补上，并各配 18 项与 9 项测试。二是**删除**：`POST /topics/{id}/run` 与 T15 已删的 `POST /agent/runs` 是同一条理由 —— 一次研究运行消耗模型预算，触发权属于调度器与操作员 CLI，不属于任意已登录客户端；`/sources` 的字段就是部署配置（`base_url` 来自 `GITHUB_API_BASE`、`credential_ref` 指向环境变量 Token），改它们走环境变量与迁移，"一个能改限流预算的 HTTP 端点等于把公共 API 的用量控制交给任意已登录客户端"。

一键入口 `.\scripts\verify_t18.ps1`（引擎不可用时 `-SqliteFixture`）。它除了静态门禁与十项比较之外，还会**故意把三份文档各改错一处**（`docs/api-contract.md` 的幽灵端点、`docs/data-model.md` 的幽灵表、`ARCHITECTURE.md` 的 `- [x] T18` 勾选）再跑一次比较、要求检查器**同时**报出三条漂移并退出 1，然后复原 —— 一个永远回答"一致"的比较器不会比它取代的"读一遍"更好，那四组幽灵接口今天还会留在契约里。三处一起改是有意的：一处漂移可能被单个检查的偶然巧合盖住，"三条一起被报出"才说明每一侧都在真的比较。

**T18 顺手修掉的两个真实缺陷**（都不是 T18 新引入的）：

- **天真时间戳在边界上被拒绝，而不是在查询构造器里炸成 `500`。** `GET /reports?created_after=2026-09-24T00:00:00`（不带时区偏移）自 T12 起就是坏的：`UTCDateTime` 会抛 `ValueError: UTC timestamps must be timezone-aware`，冒成 `500`。参数从未被行使，所以没人抓到。修复是新增 `api/params.py:UtcDateTime`（三个路由统一改用它），并让 422 的 detail 同时给出**字段路径与原因**。
- **路由遍历在不同 FastAPI 版本下答案不同。** 检查器原先直接遍历 `app.routes`，在宿主机上找到 41 条路由，在容器里（FastAPI 0.143）**一条都找不到** —— 而它随后把契约里每个端点都报成"缺失"。改为递归下钻 `routes` 与 `original_router` 两种形状，并且**拒绝相信空结果**：走到 0 条路由就直接报错，而不是当作"契约干净"。

判据与实测值见 [docs/api-contract.md](docs/api-contract.md) §3/§4/§8。

**T18 交付后的复核又修掉七处缺陷**（探针先行：每条都先写一个**能失败**的测试或脚本证明它真实存在，再落最小修复）：

- **`q` 是子串搜索，就要把 `%` / `_` 当字符。** 实现原把调用方输入直接拼进 `LIKE`，于是 `q=_` 匹配每个至少一字符的仓库、`q=%` 匹配整份目录 —— 与契约 §4 说的"子串匹配"正好相反。新增 `_literal_like()` 转义并给 `like(...)` 传 `escape`，补一条同时钉住两个方向的回归测试（转义过头也会失败）。
- **冒烟的清理删不到"匿名拒绝"留下的行。** 未认证请求写下的 `auth.token` / `failed` 行 `actor_id` 为 `NULL`，按 actor 过滤**永远够不到**，每次运行都在只增不删的账本上永久留 3 行（Compose 库上已积累 24 行）。改为**字面前缀 + 请求 id** 两条句柄（`X-Request-ID` 由中间件写进审计行），并新增 Criterion 17 断言清理之后的 `leftovers` 四项全 `0`。
- **比较器的结论不能被"把输入改窄"放大。** `check_docs_drift.py --only endpoints` 原会报 `{"passed": true}` 并退出 `0`（"跑过的都过了"被读成"文档一致"）。载荷补 `run` / `total` / `complete`，**十项全跑且全过才退 `0`**。
- **断言里的日期不能是"写脚本那天"的日期。** 快照窗口断言原比对硬编码的 `2026-10-13,…`，而窗口由时钟推出，该断言只在当天成立；改为与冒烟报出的 `requested_window` 比较。

其余三处（`LIKE` 前缀越界删除、载荷里写常量而非观测、docstring 与实现相矛盾的排序说明）见 `ARCHITECTURE.md` §9.15。

`ARCHITECTURE.md` §14 的实施任务清单已无剩余项。


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

T17 验证命令：

```powershell
.\scripts\verify_t17.ps1

# 本机 Docker 引擎起不来时的等价路径：同一份演练 + 同一套断言，跑在真实 SQLite 文件上
.\scripts\verify_t17.ps1 -SqliteFixture

# 演练残留行未清干净时单独收尾
D:\python\ana\python.exe scripts\t17_security_drill.py --cleanup-only
```

该脚本在真实应用与真实数据库上跑完四类演练：**越权**（三角色 × 四种可见范围，匿名 `401`/`303`，列表 `total` 与可见 id 一致，四类写拒绝全 `403`，拒绝响应不含标题而审计行带被拒资源 id）、**Prompt Injection**（双语注入混在正常 README 里，中英两路均未到达模型、普通文本存活、命中即停转人工复核、五个白名单工具可达且 Prompt 内零凭据）、**API 失败**（源 `5xx`/超时各重试 3 次落 `COLLECTOR_FAILED`、模型不可用落 `AGENT_MODEL_UNAVAILABLE`、推送 `failed` → 换渠道 `sent` 且重放不二次发送）、**重复任务**（两次采集/两次扫描/两次推送无副作用）。判据与实测值见 [docs/security-and-privacy.md](docs/security-and-privacy.md) §9。

两个引擎上均已实跑，payload 逐值一致：`-SqliteFixture`（2026-10-15）与默认 Compose + 真实 MySQL 路径（2026-10-15，含 `alembic upgrade head` 12 个迁移 + `alembic check` → `No new upgrade operations detected`）。

T18 验证命令：

```powershell
.\scripts\verify_t18.ps1

# 本机 Docker 引擎起不来时的等价路径：同一份冒烟 + 同一套断言，跑在真实 SQLite 文件上
.\scripts\verify_t18.ps1 -SqliteFixture

# 只跑十项文档一致性比较（`--only` 可重复，取值即上表的检查名）
D:\python\ana\python.exe scripts\check_docs_drift.py
D:\python\ana\python.exe scripts\check_docs_drift.py --only endpoints --only tables

# 冒烟残留行未清干净时单独收尾
D:\python\ana\python.exe scripts\t18_mysql_smoke.py --cleanup-only
```

该脚本做三件事：静态门禁（pytest / Ruff check+format / mypy / `docker compose config`）→ **十项文档一致性比较**（并故意把 `docs/api-contract.md`、`docs/data-model.md`、`ARCHITECTURE.md` 各改错一处，要求检查器**同时**报出三条漂移、退出 `1`，再复原后重新通过）→ 在真实数据库上冒烟 T18 新交付的两组端点（仓库目录与快照、审计日志），断言段覆盖 17 条判据。冒烟按名字前缀清理自己的行，因此**不要求全新数据库**，可在有数据的库上重复执行；第 17 条判据断言清理之后的 `leftovers` 四项全 `0` —— 把脚本末尾那句"把库留在原样"从注释变成测量。

两个引擎上均已实跑（2026-10-15）：`-SqliteFixture` 与默认 Compose + 真实 MySQL 路径，payload 逐值一致。

> **T11–T16 的容器路径需要在全新数据库上跑。** 这些脚本断言的是**绝对计数**（`runs_before == 0`、`pipeline_total == 8`、`report_total == 2`……），持久卷里残留上一轮的行就会失败（症状如 `expected '8' but got '18'`）。逐条验证前先清库：
>
> ```powershell
> docker compose --env-file .env.example -f infra/docker-compose.yml down -v
> ```
>
> 六个脚本在真实 Compose 五服务 + 真实 MySQL 上均已实跑通过（2026-10-14）。**T17 不同**——它按主键高水位只删除自己新建的行，因此在有数据的库上也能跑（这一点是刻意的：演练要能在被观察的系统上安全地重复执行）。


## 开发原则

1. 数据采集和指标计算保持确定性，Agent 负责研究决策、证据补齐、验证和综合写作。
2. 常规低风险周报采用 `AUTO_WITH_DELAY`，不为追求安全而默认阻塞人工审核。
3. 企业权限、审计、模型数据保护和 Token 成本以轻量方式实现，保留扩展接口，不宣称完成完整 DLP、ABAC 或多租户。
4. 每次代码变更若影响架构、接口、数据模型、依赖或目录结构，必须同步回写 `ARCHITECTURE.md`。
