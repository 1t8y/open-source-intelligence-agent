# 项目接口基线

> 文档状态：实施基线（T1-T23 已对齐）
> 文档版本：v0.16
> 最后更新：2026-10-16
> 最终版路径：`D:\job-workspace\project_sources\open-source-intelligence-agent`

## 1. 契约范围

本文件是接口索引和跨模块基线，详细契约分为三层：

| 层级 | 详细文档 | 用途 |
|---|---|---|
| 外部 HTTP API | [docs/api-contract.md](docs/api-contract.md) | 浏览器和 API 客户端调用 |
| Agent Tool | [docs/agent-tool-contract.md](docs/agent-tool-contract.md) | Agent 内部受限工具调用 |
| 数据结构 | [docs/data-model.md](docs/data-model.md) | 数据库字段、关系和约束 |

若本文件与详细契约冲突，以本文件确定边界，再同步修正详细文档。

## 2. 通用接口原则

- API 前缀为 `/api/v1`，返回 JSON，错误使用 RFC 7807 Problem Details。
- 时间在接口和数据库中统一使用 UTC ISO 8601，展示层转换为 `Asia/Shanghai`。
- 所有资源 ID 使用整数；所有列表接口分页，默认 `page_size=20`，最大 100。
- 写操作支持 `Idempotency-Key`；创建异步任务返回 `202 Accepted`。
- 所有接口执行身份认证、RBAC 和资源 ACL。
- 外部 API 字段名、Agent Tool 字段名和数据库模型字段名必须逐字一致，不能依靠应用层猜名转换。

## 3. 外部 API 资源

| 资源组 | 代表接口 | 权限 | 关键契约 |
|---|---|---|---|
| 系统 | `GET /` | 公开 | 浏览器预览重定向到 `/docs` 或 `/health` |
| 系统 | `GET /health` | 公开 | 进程存活状态、服务名和版本 |
| 认证 | `POST /auth/login`、`GET /auth/me` | 登录用户 | JWT/Cookie、角色和数据范围 |
| 数据源 | **无 HTTP 端点** | 部署/运维 | 行由采集器按 `(source_type)` 幂等自供给（`collectors/service.py:ensure_data_source`）；`base_url` / `credential_ref` / 限流预算属**部署配置**，改它们走环境变量与迁移，不走请求处理器 —— 一个能改限流预算的端点等于把公共 API 的用量控制交给任意已登录客户端 |
| 主题 | `/topics`、`/topics/{id}` | 读：任何已登录用户（`enabled` 全角色、禁用主题仅 ADMIN）；写：ADMIN | 增删改此前无 HTTP 面，T18 记为"设计未做"，T19 先补本节契约再实现。可见性规则与仓库目录的 `topic_id` 过滤是**同一个** `can_read_topic`，没有第二套；列表过滤在数据库查询里完成，`total` 与 `items` 同条件。写只给 ADMIN：主题决定盯什么，也就决定下一次每日运行消耗多少模型预算与公共 API 配额，与数据源拒绝 `POST /sources` 同一条理由；`EDITOR` 的写权限仍只覆盖报告。**触发研究仍不在 API 上**（T15 的 `POST /agent/runs` 同一条理由，一次运行消耗模型预算，触发权属调度器与操作员 CLI） |
| 仓库 | `/repositories`、`/repositories/{id}/snapshots` | ADMIN/EDITOR/VIEWER | 仓库元数据为公开级（`security-and-privacy.md` §2），不做逐行 ACL；`topic_id` 过滤要求主题可见（`enabled` 对全角色、禁用主题仅 ADMIN），越权 `403` + `repository.read` 审计。快照窗口是**闭区间**，与 `get_snapshot_series` 一致 |
| Agent | `GET /agent/runs`、`/agent/runs/{id}`、`/agent/runs/{id}/steps` | ADMIN/EDITOR | **只读**（运行由 pipeline/worker 创建，不经 API）；列表按 `status`/`topic_id` 过滤并分页，轨迹含未挂到步骤的工具调用 |
| 报告 | `/reports`、`/{id}/verify`、`/{id}/review`、`/{id}/publish`、`/{id}/rollback`、`/{id}/push`、`/publish-due` | 读 ADMIN/EDITOR/VIEWER，写 ADMIN/EDITOR | 版本、状态机、ACL 和审核 |
| 推送 | `/push/targets`、`/push/records`、`/push/retry` | ADMIN/EDITOR | 投递允许列表、投递记录与失败重试扫描 |
| 管理界面 | `GET /ui/{reports,reports/{id},pushes,topics,topics/{id},repositories,runs,evaluations,dashboard,dashboard/runs/{id},audit,login}`；`POST /ui/reports/{id}/{verify,review,publish,rollback,edit,push}`、`POST /ui/reports/publish-due`、`POST /ui/topics`、`POST /ui/topics/{id}/{update,toggle,delete}`、`/ui/login`、`/ui/logout` | 与同一资源在 API 侧完全相同的角色常量（`REPORT_READ_ROLES` / `REPORT_WRITE_ROLES` / `TOPIC_READ_ROLES` / `TOPIC_WRITE_ROLES` / `REPOSITORY_READ_ROLES` / `METRICS_ROLES` / `AUDIT_READ_ROLES`） | 服务端渲染页面，复用同一套服务层；登录态为「左侧分组侧栏 + 右侧内容」，侧栏条目与路由准入由同一组常量生成（菜单列出而打开被拒 = 缺陷）；侧栏与内容之间的边界可调（鼠标 / 方向键 / `localStorage`）；会话走 `HttpOnly` Cookie，写操作走 CSRF 双提交；写成功 `303` 重定向、失败原地重渲染并回填；推送渠道取自目标行而非表单；运行 / 评测的触发不在界面上（同 API 的理由）；**运维配置（推送目标白名单、用户账号）只从后端写入，界面上的提及仅对 `ADMIN` 渲染**（`OPS_CONFIG_ROLES`；非管理员时这份 allowlist 连查询都不执行） |
| 评测 | `/evaluations`、`/evaluations/runs`、`/evaluations/runs/{id}`、`/evaluations/{dataset_id}/cases`、`/evaluations/{dataset_id}/run`、`/evaluations/{dataset_id}/comparison` | ADMIN/EDITOR（全部，含读） | 数据集版本、样本、按变体执行与统一对比报告；未知变体 `400`、版本冲突 `409`、关闭时 `503` |
| 监控 | `/metrics/overview` | ADMIN/EDITOR | 纯只读的运维总览：任务、Token、延迟、失败率、报告人工阻断率与最新一轮评测裁决；`window_days` 1-90 越界 `422`（不夹取），窗口半开，未行使的指标为 `null` 而非 `0`，评测段不随窗口变化 |
| 回填 | **无 HTTP 端点**；CLI `backfill --action start\|resume\|status\|list`、Celery `run_backfill` | 部署/运维（无 API 角色） | 一次性、昂贵、对共享公共 API 施压的历史走查，因此只服务操作员而不暴露给请求处理器；`target_pages` 与 `per_page` 均为 `1..100`，`--provider` 一次只走一个源（无 `all`）；作业行即检查点，可断点续传；操作失误（越界、并发占用、不可续传、源不可分页）以非零码退出并打印原因，走查失败打印游标停在哪后退出，作业保持可续传 |
| 审计 | `/audit-logs` | ADMIN | **只读，不可修改**：不提供修改与删除端点，而不是用权限挡住它们；支持按 `actor_id` / `action` / `resource_type` / `resource_id` / `result` / 时间过滤与分页；`metadata_json` 不出站（未定义允许键的 JSON 不该放在 egress 上） |

完整请求、响应和错误示例见 [api-contract.md](docs/api-contract.md)。

内部应用入口：

```text
create_app(settings: Settings | None = None) -> FastAPI
```

传入 `settings` 时使用显式配置，不读取进程级缓存；未传入时调用 `get_settings()`。该入口供测试和 Uvicorn `--factory` 使用。

## 4. 内部模块接口

模块依赖方向：

```text
api/web
  -> application services
    -> collectors/pipeline/agent/reports/observability
      -> persistence/core
```

跨层接口必须通过明确的请求对象和结果对象，不允许 Web 层直接调用 SQL，也不允许 Agent 直接访问数据库 Session。

| 模块 | 对外能力 | 主要消费方 |
|---|---|---|
| `collectors` | `collect_source`、返回标准仓库数据；`PaginatedRepositoryCollector`（`collect_page` / `describe_query`）、`RepositoryContentSource`（`fetch_readme`）与 `RateLimiter` | pipeline/workers/backfill |
| `pipeline` | 清洗、快照、候选排序、仓库正文缓存预热、Pipeline 运行记录与编排 | workers/api |
| `backfill` | 作业生命周期与游标持久化（`jobs.py`）、走查本体（`service.py:run_backfill` / `start_backfill` / `resume_backfill`）、CLI 四动作 | workers/cli |
| `agent.runtime` | `run_research_agent`、运行编排与 `AgentRunResult` | workers |
| `agent.graph` | 有界状态图 `plan→tool→observe→verify→write` | agent.runtime |
| `agent.llm` | `ModelClient` 协议与两个 Adapter（离线桩 / OpenAI-compatible）、消息契约 | agent.graph |
| `agent.prompts` | 版本化 Prompt 资产发现与加载 | agent.graph |
| `agent.profile` | 执行档案 `ExecutionProfile` 与消融开关，`profile_for_target` 映射 | evaluation/api |
| `agent.tools` | 白名单工具 | agent.runtime |
| `reports` | 草稿、验证、策略决策和发布 | api/workers |
| `security` | 登录、授权、脱敏和审计 | 所有模块 |
| `evaluation` | 数据集版本化与导入、五个评测变体、确定性 Scorer、执行与统一对比报告 | api/cli |
| `observability` | 纯聚合（`metrics.py`，不碰数据库）、读侧查询（`queries.py`：总览、运行账本与轨迹）、领域错误 | api/web/cli |
| `workers` | Celery 应用、周期任务和 Worker 入口 | 部署/调度 |
| `persistence` | Repository 和事务边界 | 所有业务模块 |
| `migrations` | Alembic 升级、回滚和 schema drift 检查 | 开发/部署 |

Worker 接口：

```text
create_celery_app(settings: Settings | None = None) -> Celery
build_beat_schedule(settings: Settings) -> dict[str, dict[str, Any]]
healthcheck() -> {"status": "ok", "task": "healthcheck"}
run_daily_pipeline(provider="all", limit: int | None = None, business_date: str | None = None) -> dict
run_backfill(provider: str = "github", target_pages: int = 5, per_page: int = 50, job_id: int | None = None) -> dict
```

`run_backfill` 是唯一**不进 beat 排程**的任务：它是一次性的、昂贵的、对共享公共 API 的安全压力，触发权属于操作员。`job_id` 只由重试设置，此时 `target_pages` / `per_page` 被忽略 —— 作业行自己带着它们，重试不允许放宽操作员选定的预算。`time_limit` 为 1800 秒、软超时 1740 秒，因为按源预算行走的走查会合理地超过 worker 的五分钟默认值。

Celery CLI 入口为 `open_source_intelligence.workers.celery_entry:app`。Beat 每 60 秒执行一次 `open_source_intelligence.workers.tasks.healthcheck`；`PIPELINE_SCHEDULE_ENABLED` 为真时，每天 `PIPELINE_SCHEDULE_HOUR:PIPELINE_SCHEDULE_MINUTE`（默认 02:00，按 `APP_TIMEZONE`）执行一次 `open_source_intelligence.workers.tasks.run_daily_pipeline`。

`run_daily_pipeline` 任务失败时按 `PIPELINE_TASK_MAX_RETRIES` 重试，退避为 `PIPELINE_RETRY_BACKOFF_SECONDS * 2^已重试次数`；每次尝试写入独立的 `pipeline_runs` 行，`attempt` 取 `已重试次数 + 1`。参数非法（非 `github/huggingface/all` 或 `limit` 越界）直接失败且不重试，因为重试不会改变结果。

Persistence 接口：

```text
create_database_engine(database_url: str, *, echo: bool = False) -> Engine
create_session_factory(engine: Engine) -> sessionmaker[Session]
Base.metadata
```

首期模型覆盖 `users`、`roles`、`user_roles`、`audit_logs`、`data_sources`、`topics`、`repositories`、`repo_snapshots`、`topic_repositories` 和 `pipeline_runs`；T7 追加 `agent_configs`、`agent_runs`、`agent_steps`、`tool_calls`、`reports`、`report_sources`、`report_drafts`、`report_sections` 和 `repository_contents`；此后 T10 追加 `report_versions`，T11 追加 `report_verifications`、`report_reviews`，T13 追加 `push_targets`、`report_pushes`，T14 追加 `eval_datasets`、`eval_cases`、`eval_runs`、`eval_results`，T16 追加 `backfill_jobs`，元数据共 **29 张表**（`feedback` 属计划内、尚未实现）。

Security 接口：

```text
hash_password(password: str) -> str
verify_password(password_hash: str, password: str) -> bool
create_access_token(user: User, settings: Settings) -> str
decode_access_token(token: str, settings: Settings) -> AccessTokenClaims
authenticate_user(session: Session, username: str, password: str) -> User | None
record_audit_event(session: Session, ...) -> AuditLog
get_current_user(...) -> User
require_roles(*allowed_roles: str) -> Callable[..., User]
can_read_report(user: User, report: Report) -> bool
readable_reports_filter(user: User) -> ColumnElement[bool]
require_report_read(session, request, user, report) -> Report
deny_request(session, request, *, action, resource_type, resource_id=None, ...) -> None
```

认证接口：`POST /api/v1/auth/login`、`GET /api/v1/auth/me`。RBAC 每次请求从数据库读取当前角色，不依赖 Token 中的历史角色快照。

报告授权有**一对**入口，规则一致但用途不同：`can_read_report` 判单行（Python 谓词），`readable_reports_filter` 判集合（SQL 谓词，必须进 `WHERE`）。列表页用后者，否则 `total` 与翻页位置会泄露无权报告的存在。`require_report_read` 是写路径的对象门：通过则返回该行，越权则经 `deny_request` 写审计后抛 `403`。`deny_request` 的 `resource_id` 可空 —— 请求域拒绝（缺 Token、缺角色）本来就没有资源可命名，而资源域拒绝必须能说清拒了哪一条，因此 `report.read` / `report.verify` 这类动作一律带 `resource_id`。

Collector 接口：

```text
GitHubCollector.collect(limit: int) -> list[CollectedRepository]
HuggingFaceCollector.collect(limit: int) -> list[CollectedRepository]
collect_repositories(session, collectors, limit) -> CollectionSummary
upsert_repository(session, source, item) -> Repository
```

默认 API 地址为 `https://api.github.com` 和 `https://hf-mirror.com/api`，可通过 `GITHUB_API_BASE`、`HUGGINGFACE_API_BASE` 覆盖。API Token 可选，空白 Token 会规范化为未配置。

MySQL 写入路径使用原生 Upsert，`data_sources.source_type` 和 `repositories(source_id, provider_repo_id)` 均由数据库唯一约束保证并发幂等。

Pipeline 接口：

```text
create_daily_snapshots(session, *, snapshot_date: date) -> int
get_snapshot_series(session, repository_id, *, start_date: date, end_date: date) -> Sequence[RepoSnapshot]
rank_topic_candidates(session, topic, *, as_of_date: date) -> list[RankedCandidate]
rank_all_topics(session, *, as_of_date: date) -> dict[int, int]
```

CLI 入口为 `python -m open_source_intelligence.pipeline.cli`，支持 `snapshot`、`rank`、`all`、`daily` 和 `runs`。快照按 `(repository_id, snapshot_date)` 幂等；首日增量为 `NULL`，后续与最近历史快照求差。候选筛选只使用语言、关键词、最低 Star 和时间窗口；分数为 `max(0, star_growth + 2 * fork_growth + latest_stars / 1000)`。重复排序会替换该主题的现有候选链接，失败或不再命中过滤条件的旧候选不会残留。

Agent Guard 接口：

```text
inspect_untrusted_text(text: str) -> GuardDecision
sanitize_untrusted_text(text: str) -> tuple[str, GuardDecision]
guard_tool_payload(payload: Any) -> tuple[Any, GuardDecision]
```

`AGENT_MAX_TOKENS` 是单次 Agent 运行的 Token 上限；达到 80% 时停止新增研究规划调用，达到 100% 时不再发起任何模型调用。不可信内容命中指令模式时，工具结果中的命中行替换为 `[BLOCKED_UNTRUSTED_INSTRUCTION]`，运行写 `AGENT_PROMPT_INJECTION` 并进入人工复核。

Report 接口：

```text
resolve_citation(session, citation: CitationRef) -> ResolvedCitation
generate_report(session, *, agent_run_id: int, title: str | None = None, now: datetime | None = None) -> ReportGenerationResult
```

CLI 入口为 `python -m open_source_intelligence.reports.cli`，动作为 `--action {generate,verify,publish,review,publish-due,push,push-retry}`（`--agent-run-id` 单独使用等价于 `generate`，保持 T10 入口兼容）。报告只从最新章节草稿合成；六个章节必须齐全且每节至少一条引用，含数字的章节至少一条 `metric` 引用。引用必须解析到 `repo_snapshots`、`repository_contents` 或已发布 `reports`。重复生成相同内容不新增版本，内容变化时递增 `reports.current_version` 并写入 `report_versions`。`push` 额外接受 `--channel {email,webhook} --target-id <id>`，`push-retry` 接受 `--limit <n>`。

推送接口（T13，`notifications.service` 是唯一出站入口）：

```text
push_report(session, *, report_id, channel, target_id, actor_id=None, settings=None, notifier=None, now=None) -> PushOutcome
push_after_publish(session, *, report_id, actor_id=None, settings=None, notifier=None, now=None) -> list[PushOutcome]
retry_push(session, *, push_id, actor_id=None, settings=None, notifier=None, now=None) -> PushOutcome
retry_failed_pushes(session, *, limit=100, actor_id=None, settings=None, notifier=None, now=None) -> list[PushOutcome]
list_targets(session, *, channel=None) -> Sequence[PushTarget]
list_pushes(session, *, report_id=None, status=None, limit=50) -> Sequence[ReportPush]
build_message(session, report) -> OutboundMessage
build_channel(settings, channel) -> NotificationChannel
```

`push_report` 的守卫顺序固定为 **状态（`approved` / `published`）→ `CONFIDENTIAL` 需同版本 `approve` 审核 → 允许列表（启用 + 渠道一致 + `allow_private` / `allow_confidential`）**，任一违规抛 `PushError` 子类。传输失败**不抛异常**，返回 `status="failed"` 的 `PushOutcome` 并落库；`push_after_publish` 吞掉一切异常（写 `report.push` / `denied` 审计），因为它挂在发布链路里。`build_channel` 在 `live` 模式缺 `push_smtp_host` / `push_email_from` 时抛 `PushTargetNotAllowed`，且发生在写行之前。

Evaluation 接口（T14）：

```text
# 数据集（dataset.py）
parse_dataset(document: Any) -> DatasetSpec
load_dataset_file(path: str | Path) -> DatasetSpec
discover_dataset_files(directory: str | Path = "datasets") -> list[Path]
import_dataset(session, spec: DatasetSpec, *, created_by: int | None = None) -> DatasetImportResult
resolve_dataset(session, *, dataset_id=None, name=None, version=None) -> EvalDataset
list_datasets(session, *, limit=50) -> Sequence<EvalDataset>
list_cases(session, *, dataset_id: int, enabled_only: bool = False) -> Sequence[EvalCase]

# 目标变体（targets.py）
profile_for_target(target_type: str) -> ExecutionProfile | None      # agent/profile.py
run_target(session, *, case: EvalCase, target_type: str, settings, client=None, now=None) -> VariantOutput

# 评分（scorers.py）
score_case(session, *, case: EvalCase, output: VariantOutput) -> ScoredCase
score_failed_case(*, case_key, status, error_code, error_message) -> ScoredCase

# 运行与报告（runner.py）
resolve_targets(target_types: Sequence[str] | None) -> tuple[str, ...]
run_evaluation(session, *, dataset_id=None, name=None, version=None,
               target_types: Sequence[str] | None = None, settings=None,
               client=None, now=None) -> ComparisonReport
aggregate_run(session, *, run: EvalRun) -> TargetSummary
build_comparison(session, *, dataset: EvalDataset, runs: Sequence[EvalRun]) -> ComparisonReport
latest_runs_per_target(session, *, dataset_id: int) -> list[EvalRun]
list_runs(session, *, dataset_id=None, target_type=None, limit=50) -> Sequence[EvalRun]
compare_latest(session, *, dataset_id: int) -> ComparisonReport
collect_datasets(session, *, limit=50) -> Sequence[EvalDataset]
```

```text
python -m open_source_intelligence.evaluation.cli --action {list,import,run,compare}
  [--dataset-file PATH] [--dataset-dir DIR] [--dataset-id ID]
  [--name NAME] [--version VERSION] [--target TARGET ...] [--limit N] [--actor-id ID]
```

`--action run` 是唯一真正执行 Agent 的动作；`list` 与 `compare` 只读，供运维在无 API 的情况下查看数据集与最近一次对比。每个动作**打印一个 JSON 文档**，与其他 CLI 一致；失败时打印 `{action, code, error}` 并 `exit 1`，`code` 是稳定错误码，脚本按它分支而无需匹配英文文案。

`TARGET_ORDER` 逐字等于 `EVAL_TARGET_TYPES`：`baseline_single_prompt`、`agent_full`、`agent_no_verifier`、`agent_no_tools`、`agent_no_history`。`resolve_targets(None)` 返回全部五个；传入未知值抛 `UnknownTarget`；传入合法子集时按 `TARGET_ORDER` 归一化排序，因此报告顺序不随调用方参数顺序变化。

**参照链与发布裁决**：`TargetSummary.reference_target` 记录该变体的参照对象 —— `agent_full` 参照 `baseline_single_prompt`，三个消融参照 `agent_full`，`baseline_single_prompt` 无参照（`None`）。`deltas_vs_reference` 是与该参照的差值。`ComparisonReport.passed` 只裁决 `PROMOTION_TARGETS = ("agent_full",)`：消融是对照而非候选，其损失只通过自身差值呈现，不会否决发布；无候选的 sweep 按其包含的全部变体评判。**`passed` 额外要求 `scored_case_count > 0`**，未产生任何可评分样本的变体不能通过。

**指标分层**：`COLUMN_METRICS = ("citation_coverage", "factual_consistency", "task_completion")` 落真实列；`DETAIL_METRICS = ("source_accuracy", "tool_success_rate")` 与运行级信号（`tool_call_total`、`tool_call_denied`、`verification_sufficient_rate`）存 `details_json`。未行使的指标为 `None`，聚合时被剔除均值之外，绝不计作 `0`。

Evaluation 错误码（`evaluation/errors.py`，`EvaluationError` 为基类，API 统一映射为 RFC 7807，CLI 打印 `code`）：

| 错误码 | 场景 | API 状态 |
|---|---|---:|
| `DATASET_INVALID` | 数据集文件缺字段、类型错或内部不自洽 | 400 |
| `UNKNOWN_TARGET` | 目标变体不在五个之内 | 400 |
| `DATASET_VERSION_CONFLICT` | 同 `name` + `version` 内容不同（版本不可覆盖） | 409 |
| `EVALUATION_DISABLED` | `eval_enabled=false`，评测层被关闭 | 503 |
| `NOT_FOUND` | 数据集、样本或 `eval_run` 不存在 | 404 |

CLI 的 `NOT_FOUND` 还覆盖 `run_evaluation` 遇到"数据集没有启用样本"的情形；API 侧同一情况同样渲染为 `404 NOT_FOUND`。

Pipeline 运行记录接口：

```text
start_pipeline_run(session, *, run_type, topic_id=None, attempt=1, metrics_json=None) -> PipelineRun
mark_pipeline_run_running(session, run, *, started_at=None) -> PipelineRun
finish_pipeline_run(session, run, *, status, metrics_json=None, error_code=None, error_message=None) -> PipelineRun
fail_pipeline_run(session, run, *, error_code, error_message, metrics_json=None) -> PipelineRun
get_pipeline_run(session, run_id) -> PipelineRun | None
list_pipeline_runs(session, *, topic_id=None, run_type=None, status=None, limit=20) -> Sequence[PipelineRun]
summarize_pipeline_run(run) -> PipelineRunSummary
```

`run_type` 取值为 `collect`、`snapshot`、`rank`、`contents`、`agent`、`publish`、`push`；`status` 取值为 `queued`、`running`、`succeeded`、`failed`、`partial`。`agent`、`publish` 和 `push` 必须提供 `topic_id`，与数据库 CHECK 约束一致。运行记录的每次状态变化立即提交，避免进程崩溃后丢失失败证据；`error_message` 写入前经过脱敏并截断到 2000 字符。

每日确定性 Pipeline 编排接口：

```text
run_collect_stage(session, *, business_date, collectors, limit, attempt=1) -> CollectStageOutcome
run_snapshot_stage(session, *, business_date, attempt=1) -> StageOutcome
run_rank_stage(session, *, topic, business_date, attempt=1) -> StageOutcome
run_rank_all_topics(session, *, business_date, attempt=1) -> list[StageOutcome]
run_contents_stage(session, *, business_date, sources, limit, refresh_after=None, attempt=1) -> ContentsStageOutcome
run_daily_pipeline(session, *, business_date, collectors, limit, content_sources=(), content_limit=0, attempt=1) -> DailyPipelineResult
```

固定顺序为 `collect -> snapshot -> rank -> contents`，每个阶段各写一条运行记录。采集阶段只有在所有供应商都失败时才抛 `PipelineStageError`（错误码 `COLLECTOR_FAILED`）并停止批次；部分失败记为 `partial` 后继续。快照失败抛 `PipelineStageError`（`SNAPSHOT_FAILED`）。单个主题的排序失败只把该主题记为 `failed`（`RANK_FAILED`），不阻塞其他主题，整体状态为 `partial`。排序阶段不调用大模型。

`contents` 阶段把仓库正文（当前为 README）写进 `repository_contents` 缓存，排在 `rank` 之后是因为它只需要"哪些仓库值得预热"这一个排名结果。候选集合 = 启用主题下 `selected_for_agent=true` 的候选链接，按 `(topic_id, rank)` 去重后取前 `limit` 个；`content_sources` 为空时整个阶段跳过、不写运行记录，而 `run_contents_stage` 收到空列表则直接 `ValueError`（"没人能回答这个问题"不该在账本上留一条绿色的行）。抓取结果分五类计数：`fetched`（写了新内容）、`skipped`（`fetched_at >= refresh_after`，`refresh_after` 为 `None` 时不存在此类）、`missing`（源明确答复"没有 README"，如 `404`）、`unsupported`（候选的 provider 没有对应的 content source）、`failed`（源拒绝或超时）。单个仓库失败**不**抛 `PipelineStageError`：`failed` 大于零而其余四类之和为零时该运行记为 `failed`（`CONTENT_FETCH_FAILED`），部分失败记为 `partial`，其余记为 `succeeded`；没有任何候选时也是 `succeeded` 且各项计数为 `0`。这与"源拒绝一个仓库不该终止当天批次"一致，同时让"一个都没抓到"在读账本时与"抓到了几个"区分得开。阶段自身跑不起来（候选查询失败等）才抛 `PipelineStageError`，并带上已经落库的那条运行行 id。

Agent Tool 接口：

```text
ToolContext {
  agent_run_id: integer
  topic_id: integer
  authorized_topic_ids: set<integer>
  allowed_tools: list<string>
  max_tool_calls: integer
  tool_call_count: integer
}

build_default_registry() -> ToolRegistry
ToolRegistry.tool_names -> tuple<string, ...>
ToolRegistry.invoke(name, ctx: ToolContext, session, arguments: Mapping) -> ToolResult<T>
```

`ToolContext` 由运行时注入，`agent_run_id`、`topic_id`、`authorized_topic_ids`、`allowed_tools` 和工具预算都不接受模型参数伪造。`ToolRegistry.invoke()` 的执行顺序固定为「白名单 → 运行策略 → 工具预算 → Pydantic 严格校验 → handler → 只读工具写保护 → 记录 `tool_calls`」；任一失败分支都会写入一条 `tool_calls` 记录并返回 `ToolResult{ok=false, error, meta}`。

仓库类工具（`query_repo_metrics`、`get_repository_profile`）要求仓库属于 `authorized_topic_ids` 中的主题；`get_repository_content` 只读取已缓存的 `repository_contents`，不访问任意 URL；`search_prior_reports` 只返回授权主题下 `status=published` 的报告，无权限主题不泄露摘要或计数；`save_report_section` 只写当前 `agent_run` 的草稿，每个运行对应一个 `report_drafts` 行。

`repository_contents` 是 T7 新增的数据契约，字段为 `repository_id`、`content_type`（当前仅 `readme`）、`source_url`、`content`、`content_hash`、`fetched_at`，唯一键 `(repository_id, content_type)`。它的**唯一生产写入方**是 `pipeline/contents.py`（T23 之前没有任何写入方，只有演练脚本直写，于是 `get_repository_content` 恒失败、报告生成在解析 README 引用时硬失败）。写入前统一经 `clean_untrusted_text` 清洗并按 `MAX_CACHED_CONTENT_CHARS` 截断，`content_hash` 是清洗后文本的 `sha256`；内容未变时不重写该行（保持 `fetched_at` 与 `content_hash`），因此同一份未改动的 README 不会因为每天跑一次而被反复写库。`source_url` 必须是 `https` 链接（报告引用会原样带出去，`reports/citations.py` 只接受 `https`）。

Agent 运行时接口（T8）：

```text
ModelClient {
  provider: string
  model: string
  complete(messages: Sequence<ChatMessage>, tools: Sequence<ToolSchema>) -> ModelTurn
}

build_model_client(settings: Settings) -> ModelClient          # 按 AGENT_PROVIDER 选择
run_research_agent(session, *, topic_id: int, settings: Settings,
                   client: ModelClient | None = None, registry: ToolRegistry | None = None,
                   pipeline_run_id: int | None = None, window_start: datetime | None = None,
                   window_end: datetime | None = None, max_candidates: int | None = None,
                   now: datetime | None = None,
                   profile: ExecutionProfile = AGENT_FULL) -> AgentRunResult
apply_profile(configured_tools: tuple[str, ...], profile: ExecutionProfile) -> tuple[str, ...]
resolve_allowed_tools(policy: Mapping) -> tuple<string, ...>
load_or_create_config(session, settings) -> AgentConfig
load_candidate_repo_ids(session, *, topic_id: int, limit: int) -> list<int>
resolve_window(topic, *, window_start, window_end, now=None) -> tuple<datetime, datetime>
build_user_context(*, topic_id, candidate_repo_ids, window_start, window_end) -> string
```

`ModelClient` 是唯一的模型边界：`OfflineModelClient`（默认，确定性离线桩）与 `OpenAICompatibleClient` 实现同一协议，图不感知具体供应商。`resolve_allowed_tools()` 只能把 `agent_configs.tool_policy_json` 收敛为全量白名单的子集，缺失或畸形时退回全量白名单，永远不会扩大权限。

图与步骤：

```text
GRAPH_NODES = ("plan", "tool", "observe", "verify", "write")
agent_steps.step_type ∈ {plan, tool, observe, verify, write}
```

执行入口：

```text
python -m open_source_intelligence.agent.cli --action run|runs
  [--topic-id ID] [--run-id ID] [--status STATUS] [--run-limit N]
  [--window-start ISO8601] [--window-end ISO8601] [--max-candidates N]

open_source_intelligence.workers.tasks.run_research_agent(
  topic_id: int, window_start: str | None = None,
  window_end: str | None = None, max_candidates: int | None = None) -> dict
```

Celery 任务已登记但**不在** `beat_schedule` 中：Agent 消耗真实模型预算，自动编排留待报告与发布阶段就绪后开启。该任务只在 `AGENT_MODEL_UNAVAILABLE` 且未达重试上限时重试，`needs_review` 作为结果返回而不是错误。

Compose 运行接口：

```text
docker compose --env-file .env.example -f infra/docker-compose.yml up -d --build
```

服务名为 `mysql`、`redis`、`api`、`worker`、`beat`。API 仅绑定 `127.0.0.1:8000`；MySQL 和 Redis 不暴露宿主机端口，只在 Compose 内网访问。

## 5. 核心跨模块对象

实现时必须使用以下精确字段名：

```text
RepositoryRecord {
  id: integer
  source_id: integer
  provider_repo_id: string
  full_name: string
  html_url: string
  language: string | null
  stars: integer
  forks: integer
  last_seen_at: datetime
}

ResearchTask {
  task_index: integer
  intent: profile | metrics | prior_reports | content
  repo_id: integer | null
}

EvidenceRef {
  tool_name: string
  repo_id: integer | null
  ok: boolean
  result_count: integer
  truncated: boolean
  error_code: string | null
  summary: string
}

CitationRef {
  source_type: metric | readme | prior_report
  source_ref: string
  source_url: string | null
}

VerificationResult {
  sufficient: boolean
  reason: string
  covered_repo_ids: list<integer>
  missing_repo_ids: list<integer>
  content_repo_ids: list<integer>
  has_prior_baseline: boolean
}
```

`ResearchTask.intent` 使用枚举而非自由文本：`plan` 节点只能把意图映射到白名单工具，模型无法通过任务描述引入新工具。`EvidenceRef` 记录的是**工具观测**（哪个工具、是否成功、返回几条、是否截断），它取代了 T8 前草案中承担引用语义的同名对象；面向读者的可追溯引用是 `CitationRef`，由 `save_report_section` 写入草稿章节。以上对象由 `agent/state.py` 定义，`ARCHITECTURE.md` §8.2 是它们的权威说明。

```text
PublishDecision {
  mode: AUTO | AUTO_WITH_DELAY | MANUAL
  requires_review: boolean
  reason_codes: list<string>
  publish_at: datetime | null
}

AgentRunResult {
  agent_run_id: integer
  pipeline_run_id: integer
  topic_id: integer
  status: pending | running | needs_review | completed | failed
  error_code: string | null
  loop_count: integer
  tool_call_count: integer
  model_call_count: integer
  estimated_cost: decimal
  candidate_repo_ids: list<integer>
  evidence_count: integer
  section_count: integer
  report_draft_id: integer | null
  verification: VerificationResult | null
}
```

评测对象（T14）：

```text
ExecutionProfile {
  name: string
  run_verifier: boolean          # false = agent_no_verifier
  disabled_tools: frozenset<string>
}

VariantOutput {
  target_type: string
  case_key: string
  status: string                 # succeeded | failed
  error_code: string | null
  error_message: string | null
  topic_id: integer | null
  sections: tuple<SectionDraft>
  tool_call_total: integer
  tool_call_succeeded: integer
  tool_call_denied: integer
  tool_call_read: integer        # 只有读工具计入；注入面仅来自读
  prompt_tokens: integer
  completion_tokens: integer
  cost: decimal
  latency_ms: integer
  guard_triggered: boolean
  verification_sufficient: boolean | null
  agent_run_id: integer | null
  notes: tuple<string>
}

SectionDraft { section_key: string; content_markdown: string; citations: tuple<CitationRef> }

ScoredCase {
  case_key: string
  citation_coverage: decimal | null
  factual_consistency: decimal | null
  task_completion: decimal | null
  source_accuracy: decimal | null
  tool_success_rate: decimal | null
  prompt_injection_blocked: boolean | null
  cost: decimal
  latency_ms: integer
  status: string
  details: object
}

TargetSummary {
  target_type: string
  run_id: integer
  status: string
  case_count: integer
  scored_case_count: integer
  failed_case_count: integer
  metrics: map<string, decimal | null>
  total_cost: decimal
  total_tokens: integer
  mean_latency_ms: integer
  prompt_injection_block_rate: decimal | null
  tool_call_total: integer
  tool_call_denied: integer
  verification_sufficient_rate: decimal | null
  reference_target: string | null
  deltas_vs_reference: map<string, decimal | null>
  gate_failures: list<string>
  regression: boolean
}

ComparisonReport {
  dataset_id: integer
  dataset_name: string
  dataset_version: string
  case_count: integer
  targets: tuple<TargetSummary>
  replayed: boolean
  passed: boolean                 # 派生属性，只裁决 PROMOTION_TARGETS
}

MetricsOverview { window: Window; pipeline: PipelineMetrics; agent: AgentMetrics; tools: ToolMetrics; reports: ReportMetrics; evaluations: EvaluationMetrics }

Window { days: integer; start: datetime; end: datetime }        # 半开区间 [start, end)
Latency { samples: integer; p50_ms: integer | null; p95_ms: integer | null }

PipelineMetrics { total: integer; by_status: map<string, integer>; by_run_type: map<string, map<string, integer>>; success_rate: decimal | null; latency: Latency }

AgentMetrics {
  total: integer
  by_status: map<string, integer>
  by_publish_mode: map<string, integer>
  prompt_tokens: integer; completion_tokens: integer; total_tokens: integer
  tokens_per_run: decimal | null
  estimated_cost: decimal
  success_rate: decimal | null
  failure_reasons: map<string, integer>
  latency: Latency
}

ToolBreakdown { tool_name: string; total: integer; by_status: map<string, integer>; success_rate: decimal | null; latency: Latency }
ToolMetrics { total: integer; by_status: map<string, integer>; success_rate: decimal | null; by_tool: tuple<ToolBreakdown> }   # by_tool 按调用量降序
ReportMetrics { total: integer; by_status: map<string, integer>; manual_block_rate: decimal | null }

EvaluationTarget {
  target_type: string; status: string; scored_case_count: integer
  metrics: map<string, string | null>
  total_tokens: integer; mean_latency_ms: integer
  reference_target: string | null
  gate_failures: tuple<string>
  regression: boolean
}
EvaluationMetrics { dataset_id: integer | null; dataset_name: string | null; dataset_version: string | null; passed: boolean | null; regressions: tuple<string>; targets: tuple<EvaluationTarget> }

AgentRunTrace { run: AgentRun; topic_name: string | null; report_id: integer | null; steps: tuple<StepWithToolCalls>; unlinked_tool_calls: tuple<ToolCall> }
```

`ExecutionProfile` 由 `agent/profile.py` 定义，是消融的唯一开关面；`run_verifier=False` 时图仍走真实 `verify` 节点，只是恒返回 `sufficient` 并把步骤记为 `skipped`。`VariantOutput.verification_sufficient` 为 `None` 表示该变体的运行没有产生验证结论（例如运行失败），与 `false`（验证过但不充分）不同。`ScoredCase` 中所有可空比率遵循同一条约定：`null` 表示"该样本没有行使这个指标"，聚合时被剔除而不是计作 `0`。`TargetSummary.metrics` 与 `deltas_vs_reference` 的字符串化与舍入在 `to_dict()` 里统一到小数点后 4 位（`RATE_PLACES`），与 `eval_results` 的 `Numeric(5, 4)` 列一致。

监控侧的对象（`observability/metrics.py`）沿用同一条 `null` 约定，并额外固定三件事：

- **`null` 表示"没有样本"，`0` 表示"样本为零"。** 分母为零时比率是 `null`；`Latency.samples == 0` 时 `p50_ms` / `p95_ms` 也是 `null`。`estimated_cost` 是可加量，无样本时为 `0`。
- **分位数是 nearest-rank，不插值。** `p95_ms` 取排序后第 `ceil(0.95 * n)` 个样本，是某一次运行真实经历过的时长，而不是两个样本之间的数学构造；`samples` 说明这个分位数由多少观测支撑。
- **`EvaluationMetrics` 不受窗口过滤。** 它固定携带最新一轮 sweep，`passed` 直接来自评测层 `compare_latest` 的裁决，监控层不重算——看板与评测报告因此不可能给出互相矛盾的结论。从未跑过评测时 `dataset_id` 为 `null` 且 `targets` 为空，而不是报错。

`AgentRunTrace.unlinked_tool_calls` 保留 `agent_step_id` 为空的工具调用：一次在步骤落库前被预算硬停的运行，其失败证据恰在这些行里，丢弃它们会让"从失败追到具体工具调用"重新落空。轨迹与账本中的 `summary`、`result_summary` 和 `arguments` 在离开进程前一律过 `security` 的脱敏规则，与日志共用同一条实现。

详细数据库对象见 [data-model.md](docs/data-model.md)。

## 6. Agent Tool 接口

Agent 只能调用以下白名单工具：

| 工具 | 权限 | 主要输出 |
|---|---|---|
| `query_repo_metrics` | 只读 | `list[MetricPoint]` |
| `get_repository_profile` | 只读 | `RepositoryProfile` |
| `get_repository_content` | 只读、截断、不可信标记 | `ContentSnippet` |
| `search_prior_reports` | 只读、执行 ACL | `list[ReportSummary]` |
| `save_report_section` | 写当前草稿 | `SectionRef` |

工具统一返回 `ToolResult<T>`，错误码包括 `TOOL_INVALID_ARGUMENT`、`TOOL_ACCESS_DENIED`、`TOOL_NOT_FOUND`、`TOOL_TIMEOUT`、`TOOL_RATE_LIMITED`、`TOOL_BUDGET_EXCEEDED` 和 `TOOL_INTERNAL_ERROR`。

发布动作不是 Agent Tool，必须由 Policy Engine 和 Publishing Service 执行。完整契约见 [agent-tool-contract.md](docs/agent-tool-contract.md)。

## 7. 数据库接口边界

- 所有数据库访问通过 Repository 层和参数化查询。
- 数据库事务在应用服务边界开启，跨多个 Repository 时使用统一工作单元。
- 仓库唯一键为 `(source_id, provider_repo_id)`。
- 快照唯一键为 `(repository_id, snapshot_date)`。
- `pipeline_runs.attempt >= 1`，`finished_at` 不早于 `started_at`；`agent`、`publish` 和 `push` 运行必须绑定 `topic_id`。
- 每个阶段每次尝试只写一条 `pipeline_runs` 记录，禁止用同一行覆盖历史尝试。
- `agent_steps` 在运行内按 `step_index` 唯一递增。
- `agent_configs` 唯一键为 `(name, version)`；`agent_runs` 必须绑定 `pipeline_run_id`、`topic_id` 和 `agent_config_id`。
- 每个 `agent_run` 最多一个 `report_drafts` 行（`UNIQUE(agent_run_id)`）；`report_sections` 在草稿内按 `(report_draft_id, section_key, version)` 唯一，幂等键为 `(report_draft_id, section_key, content_digest)`。
- `repository_contents` 唯一键为 `(repository_id, content_type)`。
- `tool_calls` 必须关联 `agent_run_id`，只保存脱敏且截断后的参数与结果摘要；工具预算与白名单由 `agent_configs.tool_policy_json` 提供，运行时再收敛为 `ToolContext.allowed_tools`。
- 五个工具中只有 `save_report_section` 写数据且只写当前运行草稿；其余四个工具不得产生任何领域写入，由注册表的只读写保护强制并在冒烟中验证。
- `report_versions` 按 `(report_id, version)` 唯一，保存每次正式报告内容的不可变快照；`report_sources` 的引用集合**按版本归属**（`report_sources.version`），描述的是该版本的证据，因此回退到历史版本能恢复该版本自己的引用，`INV-10` 也总能对正在发布的版本校验。
- `reports.owner_id` 是 `PRIVATE` 可见范围的唯一抓手：`PRIVATE` 报告仅负责人可读，`owner_id` 为 `NULL` 时只有 `ADMIN` 可读。列表的权限过滤必须发生在数据库查询阶段，不得先取全量再在应用层过滤。
- `report_verifications` 按 `(report_id, version)` 唯一，保存该版本的验证结论（四个布尔检查 + 原因码）；重复验证刷新同一行。
- `report_reviews` 记录人对某个版本的审核决定，`decision` 取 `approve`、`reject`、`request_changes`。
- 报告编辑只允许在 `draft` / `changes_requested` 状态下进行，且只追加不可变的 `report_versions` 行，不覆盖历史版本；回退历史版本同样追加新版本，不重写历史，且只允许从 `draft` / `changes_requested` / `published` 发起（即状态机指向 `draft` 的入边）。两条路径都在写入前先判定状态，被拒时报告保持原状。
- `report_reviews.edited_version_id` 必须指向**同一份报告**的版本行：外键只保证行存在，不保证归属，服务在写审核记录前显式解析该组合。
- `MANUAL` 报告缺少 `approve` 审核记录时不能发布。
- 任何报告发布前，都必须存在针对其 `current_version` 且 `verdict=passed` 的 `report_verifications` 行（`INV-5`）。
- 发布刻意按 `publish_mode` 收紧而不放宽：`publish_mode` 是上限，验证失败、敏感内容、首次配置、运行不干净等发现只会让决策更严格。
- `publish_due_reports` 只发布 `scheduled` 且 `publish_at` 已到期的报告；`approved` 视为明确同意，跳过延迟窗口直接排队。
- `push_targets` 是报告出站的唯一允许列表：`name` 唯一，`channel` 取 `email` / `webhook`，并带 `enabled`、`allow_private`、`allow_confidential` 三个开关；投递目标只有落入该表才可送达，两个 `allow_*` 开关默认关闭。
- `report_pushes` 按 `idempotency_key` 唯一（`report:{report_id}:v{version}:target:{target_id}`），同一 `(报告, 版本, 目标)` 只有一行；`attempt >= 1`（`version >= 1` 同理），`status` 取 `pending` / `sent` / `failed`，重试只推进同一行的 `attempt`。`target_id` 为 `ON DELETE RESTRICT`（删除目标前必须先处理投递记录），`pipeline_run_id` 为 `ON DELETE SET NULL`。
- `pipeline_runs.run_type` 新增 `push` 取值，与 `agent` / `publish` 一样按 `topic_id` 归属，并同步出现在 ORM 的 `CheckConstraint`、迁移里的 CHECK 与 `pipeline/runs.py:TOPIC_RUN_TYPES` 三处。
- `pipeline_runs.run_type` 另有 `backfill` 与 `contents` 两个取值，与 `collect` / `snapshot` / `rank` 一样是**全局**运行类型（`topic_id` 为 NULL）。加值需要**四处同步**：ORM 的 `run_type_valid`、ORM 的 `topic_scope_valid`、迁移里对应的两条 CHECK、`pipeline/runs.py:GLOBAL_RUN_TYPES`；少了 `topic_scope_valid` 的表现是运行行插不进去。（加一个**按主题归属**的类型则只需两处：`run_type_valid` 与 `TOPIC_RUN_TYPES` —— `topic_scope_valid` 只列全局类型，非全局的那些天然落进它的 `OR topic_id IS NOT NULL` 分支。`backfill` 与 `contents` 走的是前一条路。）
- `backfill_jobs` 是走查的检查点，没有外键：`next_page >= 1`、`target_pages` 与 `per_page` 均限 `1..100`、`pages_completed` 与 `items_collected` 非负、`attempt >= 1`。`status` 取 `pending` / `running` / `succeeded` / `failed` / `cancelled`，`heartbeat_at` 在每一页提交时刷新 —— 它**不是** `updated_at`：SQLAlchemy 的 Python 侧 `onupdate` 在 UPDATE 时会覆盖显式赋值，用 `updated_at` 当心跳的守卫永远看不到"过期"。作业行即检查点，所以续传只需要这一行，不需要重放历史；`running` 且心跳未超过 15 分钟的作业不可被第二个走查者认领。每页的仓库与指向下一页的游标在**同一事务**提交（`ingest_items` 不提交、`advance_job` 提交），游标动过就一定有对应的行。
- `eval_datasets` 按 `(name, version)` 唯一，`eval_cases` 按 `(dataset_id, case_key)` 唯一，`eval_results` 按 `(eval_run_id, eval_case_id)` 唯一：数据集版本与逐样本测量都不可覆写，重跑刷新自身测量。
- `eval_runs.target_type` 只允许五个变体取值，`status` 只允许 `pending` / `running` / `succeeded` / `failed`，且 `finished_at >= started_at`；五个变体中只有 `baseline_single_prompt` 允许 `agent_config_id IS NULL`。
- 评测门禁指标（`citation_coverage`、`factual_consistency`、`task_completion`）落 `eval_results` 真实列（`Numeric(5,4)`）；无阈值的比率与运行级证据（`source_accuracy`、`tool_success_rate`、`prompt_injection_block_rate`、`tool_call_total`、`tool_call_denied`、`verification_sufficient_rate`）存 `details_json`。**未行使的指标以 `None` 表示，不得写成 `0`。**
- 一个变体"通过"的前提是它真的被测量过：`passed` 要求 `scored_case_count > 0`，否则全部样本失败产生的空 `gate_failures` 会被误读成"全部达标"。

## 8. 错误码与状态机

发布状态机：

```text
draft
  -> pending_review
  -> approved
  -> scheduled
  -> published
```

异常分支：`failed`；退回分支：`changes_requested`。合法迁移边为：

```text
draft             -> pending_review | scheduled | failed
pending_review    -> approved | changes_requested | failed
approved          -> scheduled | failed
scheduled         -> published | failed
changes_requested -> draft
published         -> draft
failed            -> （终态，无出边）
```

`published -> draft` 同时承载两种语义：撤回已发布的报告，以及回退到历史版本。两种动作都**追加新版本**并清空 `publish_at` / `published_at`，不重写历史；回退的具体版本号记在该版本的 `change_reason`（`rollback_to_v<n>`）里。`failed` 是唯一终态：被拒绝的报告不能通过回退复活。编辑只能发生在 `draft` / `changes_requested`，回退只能发生在 `draft` / `changes_requested` / `published` —— 两者都精确等于状态机指向 `draft` 的入边集合，仍在下游发布路径上的报告（`pending_review` / `approved` / `scheduled`）必须先显式离开该路径。状态不满足时服务在**任何写入之前**拒绝，报告保持原状（`current_version`、正文与引用集都不变）。

`pending_review` 只能由人清除（`approve` / `reject` / `request_changes`），重复评估发布策略不会把已经进入下游状态（`pending_review` / `approved` / `scheduled` / `published`）的报告静默推进。

发布决策对象 `publish_decision`：

```json
{
  "report_id": 12,
  "mode": "AUTO_WITH_DELAY",
  "resolution": "scheduled",
  "reason_codes": [],
  "requires_review": false,
  "publish_at": "2026-10-12T13:00:00+00:00"
}
```

`resolution` 取 `scheduled`（进入发布队列）或 `pending_review`（等人工）。`reason_codes` 为空表示策略没有发现任何需要收紧的问题；非空时 `requires_review` 必为 `true`。`MANUAL` 模式即使没有任何发现也恒为 `pending_review`。

常见业务错误：

| 错误码 | 场景 |
|---|---|
| `AUTH_REQUIRED` | 未登录或 Token 失效 |
| `FORBIDDEN` | 角色或 ACL 不通过 |
| `VALIDATION_ERROR` | 请求或工具参数非法 |
| `CONFLICT` | 唯一约束或状态机冲突 |
| `AGENT_BUDGET_EXCEEDED` | 超出循环、工具或 Token 预算 |
| `AGENT_PROMPT_INJECTION` | 不可信内容包含指令式文本，运行进入人工复核 |
| `PUBLISH_POLICY_BLOCKED` | 报告不满足自动发布条件（缺通过验证、`MANUAL` 缺 `approve`、窗口未到期） |
| `REPORT_STATE_INVALID` | 报告当前状态不允许该迁移或动作（含对 `failed` 报告回退） |
| `NOT_FOUND` | 报告、报告版本或 UI 资源不存在 |
| `PUSH_BLOCKED` | 报告不在可推送状态（只接受 `approved` / `published`） |
| `PUSH_APPROVAL_REQUIRED` | `CONFIDENTIAL` 报告缺少同版本的 `approve` 审核记录 |
| `PUSH_TARGET_NOT_ALLOWED` | 目标不在允许列表、已禁用、渠道不符，或未对该可见范围/敏感级别开放（含 `live` 渠道缺凭据） |
| `PUSH_DELIVERY_FAILED` | 传输层拒绝或失败，已落库为可重试结果 |
| `PUSH_DISABLED` | 推送能力被 `push_enabled` 关闭 |
| `PUSH_ERROR` | 推送领域未分类错误 |

报告资源（T12）的 HTTP 语义：

| 动作 | 成功 | 失败语义 |
|---|---|---|
| `GET /reports`、`GET /reports/{id}` | `200` | `401` 未登录；`403 FORBIDDEN` 越权（不泄露标题）；`404 NOT_FOUND` 不存在 |
| `PATCH /reports/{id}` | `200` | `409 REPORT_STATE_INVALID` 非可编辑状态；`422 VALIDATION_ERROR` 字段非法（含无法渲染为单一标题的 `title`） |
| `POST /reports/{id}/verify` | `200` | `404 NOT_FOUND` |
| `POST /reports/{id}/review` | `200` | `400 VALIDATION_ERROR` 未知决定或 `edited_version_id` 不属于该报告；`409 REPORT_STATE_INVALID` 非 `pending_review` |
| `POST /reports/{id}/publish` | `200` | `409 PUBLISH_POLICY_BLOCKED`（缺验证/缺审核）；`409 REPORT_STATE_INVALID`（状态或窗口） |
| `POST /reports/{id}/rollback` | `200` | `400` 目标为当前版本；`404` 版本不存在；`409` 报告不在可回退状态（`draft` / `changes_requested` / `published` 之外，含 `failed`） |
| `POST /reports/{id}/push` | `200`（`status` 为 `sent` 或 `failed`） | `400 PUSH_TARGET_NOT_ALLOWED`（未配置的 `live` 渠道/未知渠道）；`401` 未登录；`403 FORBIDDEN` 越权或只读角色；`404 NOT_FOUND`；`409 PUSH_BLOCKED` / `PUSH_APPROVAL_REQUIRED` / `PUSH_ERROR`；`502 PUSH_DELIVERY_FAILED`；`503 PUSH_DISABLED` |
| `GET /api/v1/push/targets`、`/records`、`POST /api/v1/push/retry` | `200` | `401` 未登录；`403 FORBIDDEN`（只读角色不可读允许列表与投递记录，也不可驱动重试） |
| UI 写操作 | `303` 重定向到目标页（POST-重定向-GET） | `400` 表单值非法（空名、非数字边界）；`403` CSRF 校验失败或角色不足；`404` 目标不存在；`409` 重名或资源仍被引用（原地重渲染并回填输入）；`303` 未登录重定向到 `/ui/login` |

UI 与 JSON API 的拒绝**共享同一组审计动作名**（T17）：`auth.csrf`（CSRF 双提交不符）、`auth.rbac`（角色不足以写）、`report.read`（角色够但报告不可见）、`topic.read`（角色够但主题不可见）。两组传输各自的拒绝都会落一条 `result="denied"` 的 `audit_logs` 行，因此"谁想读写哪一份资源"是一次查询就能回答的问题，而不是只有 API 面答得上来。审计写在**写操作**上而非传输层：只读页面渲染被拒不需要留痕，改了数据才有审计价值。

推送的 HTTP 语义有两点与直觉不同：其一，**传输失败返回 `200`**，`status="failed"` 且带稳定 `error_code`（`PUSH_DELIVERY_FAILED`），因为它是被记录的可重试结果而非请求错误 —— 与 `publish-due` 对"被拦发布"的处理一致；只有守卫违规（状态、审批、允许列表）才抛异常并映射为 `4xx`/`5xx`。其二，`400` 保留给**部署配置错误**：`live` 渠道缺 SMTP 主机或发件人时在写行之前就被拒，避免把配置问题伪装成可重试的传输失败。

回填 CLI 的退出语义（T16）。回填没有 HTTP 面，因此它的"错误语义"落在退出码与标准输出上，并且按**操作员的下一步**分流：

| 情况 | 退出码 | 输出 | 理由 |
|---|---|---|---|
| 走查成功 | `0` | `BackfillOutcome` 的 JSON（含 `pages`、`next_page`、`items_collected`、`exhausted`、`paced_seconds`） | 正常结果 |
| `status` / `list` | `0` | `{"job": ...}` / `{"jobs": [...]}`，**不触碰任何 provider** | 只读查询不应产生外部请求 |
| `BackfillLimitError`（`target_pages` / `per_page` 越界） | 非零 | 异常消息本身 | 操作失误，什么都没跑，没有作业载荷可打印 |
| `BackfillUnsupportedCollector`（源不可分页） | 非零 | 异常消息本身 | 同上，且在写库之前就拒绝 |
| `BackfillJobBusy`（游标被另一个走查者持有） | 非零 | 异常消息 + 持有起始时刻 | 操作员要等，不是要修 |
| `BackfillNotResumable`（作业已完成或无剩余页） | 非零 | 异常消息 | 幂等操作重复执行不是错误 |
| `BackfillWalkFailed`（走查中源失败） | 非零 | `{"status": "failed", "error_code", "message", "job": {...}}` | **最有价值的输出是"游标停在哪"** —— 那是操作员的下一条命令；作业保持可续传，因此打印载荷而不是抛 traceback |

失败分类沿用 `pipeline/runs.py:classify_error`：任何 `CollectorError` → `COLLECTOR_FAILED`（源拒绝），其余 → `PIPELINE_UNEXPECTED_ERROR`（我们自己的写入坏了）。`STAGE_ERROR_CODES` **故意不含** `backfill`，因为加上去只会把这两个最需要区分的信号拍平成一个码；它**含** `contents`（`CONTENT_FETCH_FAILED`），因为该阶段唯一的失败来源就是"抓不到仓库正文"。

审计结果只有 `success` / `denied` / `failed` 三个取值，比运行状态少一个 `partial`：写审计行时 `partial` 归入 `failed`（一次有仓库没抓到的刷新，落在严格的一侧）。三档之外的取值会被 `ck_audit_logs_result_valid` 拒绝，是数据库层拦住这类"拿一套词表当另一套用"的地方。

工具失败观察的形状，是模型侧与证据侧共用的契约：`tool` 角色消息的 `content` 在成功时是该工具的载荷本身，失败时是 `{"error": {"code": ..., "message": ...}}`。这两者都是 JSON 对象，所以"按类型判断"无法区分它们 —— 任何从对话里回收证据的代码必须**显式排除失败壳**，否则一次失败的读取会被记成"读到了内容"。`agent/llm/messages.py` 提供这一对定义（`tool_error_payload` / `is_tool_error_payload`），`agent/graph.py` 用它渲染失败观察、`agent/llm/offline.py:_collect_evidence` 用它过滤，两处共用同一判据以免漂移。授权失败（`TOOL_ACCESS_DENIED`）与未命中（`TOOL_NOT_FOUND`）都以失败壳到达模型，模型可以据此改换目标仓库，但不得把失败当证据。

发布策略原因码（T11，出现在 `publish_decision.reason_codes` 与 `report_verifications.reason_codes_json`）：

| 原因码 | 含义 |
|---|---|
| `VERIFICATION_FAILED` | 该版本验证未通过 |
| `LOW_CITATION_COVERAGE` | 引用覆盖率低于 `PUBLISH_MIN_CITATION_COVERAGE` |
| `SENSITIVE_CONTENT` | 正文命中凭据形态内容 |
| `SENSITIVE_REPORT` | 报告敏感级别为 `CONFIDENTIAL` |
| `AGENT_RUN_NOT_CLEAN` | 生成运行非 `completed` 或带错误码 |
| `SECURITY_RISK` | 生成运行命中 `AGENT_PROMPT_INJECTION` |
| `FIRST_RUN` | 该主题尚无已发布报告，缺少基线 |
| `MODEL_OR_PROMPT_CHANGED` | 模型、Prompt 版本或工具策略与上一份已发布报告不同 |
| `NOT_INTERNAL_VISIBILITY` | `AUTO` 模式但可见范围不是 `INTERNAL` |

Agent 运行终态与错误码（T8）：

```text
completed    证据充足、无任何错误码且 6 个草稿章节全部写入   -> pipeline_runs.status = succeeded
needs_review AGENT_NO_CANDIDATES / AGENT_BUDGET_EXCEEDED /
             AGENT_EVIDENCE_INCOMPLETE / AGENT_WRITE_FAILED /
             AGENT_PROMPT_INJECTION
             —— 结果需人工复核，不是异常              -> pipeline_runs.status = failed
failed       AGENT_MODEL_UNAVAILABLE / AGENT_DRAFT_INVALID
             —— 传输、模型或载荷失败                  -> pipeline_runs.status = failed
failed       AGENT_INTERNAL_ERROR
             —— 图内逃逸的未分类异常，运行仍被收口为终态 -> pipeline_runs.status = failed
```

`completed` 要求同时满足「无错误码」与「证据充足」，因此部分章节写入失败（`AGENT_WRITE_FAILED`）的运行不会因为证据充足而被误报为 `completed`。`needs_review` 与 `failed` 都写 `pipeline_runs.status=failed`，但只有后者值得重试：Celery 任务仅在 `AGENT_MODEL_UNAVAILABLE` 时重试。

## 9. 接口变更规则

- 新增或修改外部 API：更新 `INTERFACES.md`、`docs/api-contract.md`、OpenAPI 和测试。
- 新增或修改 Agent Tool：更新 `INTERFACES.md`、`docs/agent-tool-contract.md`、Pydantic 契约和测试。
- 修改数据库字段或约束：更新 `INTERFACES.md`、`docs/data-model.md` 和迁移。
- 修改模块依赖方向：必须新增或更新 ADR，并同步 `ARCHITECTURE.md`。
- 所有不兼容变更必须记录在 `ARCHITECTURE.md` Changelog 中。

## 10. 确认条件

开始 T1 前，用户需要确认：

1. 外部 API 的资源范围和权限模型。
2. Agent Tool 的白名单和只读/写入边界。
3. 跨模块精确字段名。
4. 风险发布状态机和 Token 预算策略。

确认后，T1 先实现项目脚手架和空服务，不先实现 Agent 或数据采集逻辑。
