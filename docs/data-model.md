# 数据模型设计

> 数据库：MySQL 8.0+  
> ORM：SQLAlchemy 2.x  
> 迁移：Alembic  
> 最后更新：2026-10-13

## 1. 设计原则

- 可查询、可聚合、可审计的数据进入结构化列；仅变化频繁且不需要单独查询的配置进入 JSON。
- Agent 轨迹、工具调用、报告来源和审计日志必须持久化，不能只写应用日志。
- 时间统一存 UTC；`snapshot_date` 使用 `DATE`，表示采集自然日。
- 所有外部 ID 同时保存供应商和供应商内 ID，避免跨平台冲突。
- 关键查询字段建立索引；唯一业务约束不能只靠应用代码判断。

## 2. 实体关系摘要

```text
users --< user_roles >-- roles
topics --< topic_repositories >-- repositories --< repo_snapshots
repositories --< repository_contents（content_type=readme）
topics --< pipeline_runs（collect/snapshot/rank/backfill 为全局运行，topic_id 为 NULL）
backfill_jobs（无外键：走查的检查点，每次尝试经 metrics_json.backfill_job_id 挂回作业）
agent_configs --< agent_runs --< agent_steps --< tool_calls
pipeline_runs --< agent_runs；topics --< agent_runs
agent_runs --< report_drafts --< report_sections
agent_runs --< reports --< report_versions
users --< reports（owner_id）
reports --< report_sources（按 report_id + version 归属）
reports --< report_reviews
reports --< report_verifications
reports --< report_pushes >-- push_targets
pipeline_runs --< report_pushes
eval_datasets --< eval_cases；eval_datasets --< eval_runs
eval_runs --< eval_results >-- eval_cases
eval_runs >-- agent_configs（baseline 的 target_type 允许为 NULL）
users --< eval_datasets（created_by）
users --< feedback（计划内、尚未实现，无 ORM 模型与迁移）
users --< audit_logs
```

## 3. 身份与权限

### `users`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK, AUTO_INCREMENT | 用户 ID |
| `username` | VARCHAR(64) | UNIQUE, NOT NULL | 登录名 |
| `password_hash` | VARCHAR(255) | NOT NULL | Argon2/bcrypt 散列 |
| `display_name` | VARCHAR(128) | NOT NULL | 展示名 |
| `status` | VARCHAR(16) | NOT NULL | `active`、`disabled` |
| `data_scope` | VARCHAR(32) | NOT NULL | 首期为 `personal` 或 `team` |
| `created_at` | DATETIME(6) | NOT NULL | UTC |
| `updated_at` | DATETIME(6) | NOT NULL | UTC |

### `roles`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | SMALLINT UNSIGNED | PK | 角色 ID |
| `code` | VARCHAR(32) | UNIQUE | `ADMIN`、`EDITOR`、`VIEWER` |
| `name` | VARCHAR(64) | NOT NULL | 角色名称 |

### `user_roles`

| 字段 | 类型 | 约束 |
|---|---|---|
| `user_id` | BIGINT UNSIGNED | PK, FK users.id |
| `role_id` | SMALLINT UNSIGNED | PK, FK roles.id |

联合主键 `(user_id, role_id)`；删除用户和角色时按业务决定级联或限制。

## 4. 数据源、主题与仓库

### `data_sources`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 数据源 ID |
| `name` | VARCHAR(64) | UNIQUE | 名称 |
| `source_type` | VARCHAR(32) | NOT NULL | `github`、`huggingface` |
| `base_url` | VARCHAR(512) | NOT NULL | API 根地址 |
| `credential_ref` | VARCHAR(255) | NULL | 密钥引用，不存明文 |
| `enabled` | TINYINT(1) | NOT NULL | 是否启用 |
| `rate_limit_per_minute` | INT | NOT NULL | 限流配置 |
| `config_json` | JSON | NULL | 非关键配置 |
| `last_success_at` | DATETIME(6) | NULL | 最近成功时间 |
| `created_at` | DATETIME(6) | NOT NULL | UTC |
| `updated_at` | DATETIME(6) | NOT NULL | UTC |

唯一约束：`data_sources.source_type` 唯一，保证每个外部供应商只有一条数据源配置。

### `topics`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 主题 ID |
| `name` | VARCHAR(128) | UNIQUE | 主题名 |
| `description` | TEXT | NULL | 主题说明 |
| `languages_json` | JSON | NULL | 语言过滤 |
| `keywords_json` | JSON | NULL | 关键词过滤 |
| `min_stars` | INT | NOT NULL DEFAULT 0 | 最低 star |
| `window_days` | INT | NOT NULL | 分析窗口 |
| `max_candidates` | INT | NOT NULL | Agent 候选上限 |
| `publish_mode` | VARCHAR(32) | NOT NULL | `AUTO`、`AUTO_WITH_DELAY`、`MANUAL` |
| `enabled` | TINYINT(1) | NOT NULL | 是否启用 |
| `created_by` | BIGINT UNSIGNED | FK users.id | 创建人 |
| `created_at` | DATETIME(6) | NOT NULL | UTC |
| `updated_at` | DATETIME(6) | NOT NULL | UTC |

### `repositories`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 内部 ID |
| `source_id` | BIGINT UNSIGNED | FK data_sources.id | 来源 |
| `provider_repo_id` | VARCHAR(128) | NOT NULL | GitHub/HF 平台 ID |
| `full_name` | VARCHAR(255) | NOT NULL | owner/name |
| `html_url` | VARCHAR(512) | NOT NULL | 项目地址 |
| `description` | TEXT | NULL | 简介 |
| `language` | VARCHAR(64) | NULL | 主语言 |
| `license` | VARCHAR(128) | NULL | 许可证 |
| `topics_json` | JSON | NULL | 标签 |
| `stars` | INT UNSIGNED | NOT NULL | 最新 star |
| `forks` | INT UNSIGNED | NOT NULL | 最新 fork |
| `first_seen_at` | DATETIME(6) | NOT NULL | 首次发现 |
| `last_seen_at` | DATETIME(6) | NOT NULL | 最近发现 |

唯一约束：`UNIQUE(source_id, provider_repo_id)`。索引：`(language, stars)`、`(last_seen_at)`。

### `repo_snapshots`

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | BIGINT UNSIGNED | PK |
| `repository_id` | BIGINT UNSIGNED | FK repositories.id |
| `snapshot_date` | DATE | NOT NULL |
| `stars` | INT UNSIGNED | NOT NULL |
| `forks` | INT UNSIGNED | NOT NULL |
| `open_issues` | INT UNSIGNED | NULL |
| `stars_delta` | INT | NULL |
| `forks_delta` | INT | NULL |
| `created_at` | DATETIME(6) | NOT NULL |

唯一约束：`UNIQUE(repository_id, snapshot_date)`。索引：`(snapshot_date, stars_delta)`。

### `repository_contents`

T7 新增。缓存经过清理的外部长文本（当前仅 README），供 `get_repository_content` 读取，工具不访问任意 URL。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 内容 ID |
| `repository_id` | BIGINT UNSIGNED | FK repositories.id, CASCADE | 所属仓库 |
| `content_type` | VARCHAR(32) | NOT NULL | 当前仅 `readme` |
| `source_url` | VARCHAR(512) | NOT NULL | 内容来源地址，必须是 `https`（报告引用会原样带出去） |
| `content` | MEDIUMTEXT | NOT NULL | 清理后的正文（`clean_untrusted_text` + 按 20000 字符截断） |
| `content_hash` | VARCHAR(64) | NOT NULL | 入库正文的 SHA-256 |
| `fetched_at` | DATETIME(6) | NOT NULL | 最近一次**确认**该内容的时刻（UTC），内容未变也会推进 |
| `created_at` | DATETIME(6) | NOT NULL | UTC |
| `updated_at` | DATETIME(6) | NOT NULL | UTC |

唯一约束：`UNIQUE(repository_id, content_type)`。CHECK：`content_type IN ('readme')`。

**唯一生产写入方是 `pipeline/contents.py` 的 `contents` 阶段**（T23）。T7 到 T22 之间这张表没有任何写入方（只有 `scripts/t7..t17_*` 的演练直写），于是 `get_repository_content` 恒返回 `TOOL_NOT_FOUND`、研究报告的验证恒为 `sufficient=false`、`reports generate` 在解析 README 引用时硬失败 `readme source not found`。写入统一走 `collectors/service.py:upsert_repository_content`：清洗、截断、算摘要都发生在这一处，正文未变时只推进 `fetched_at`（它既是"最后确认时刻"，也是 `contents` 阶段幂等窗口的判据）。

### `topic_repositories`

主题与候选仓库的关系表，字段：`topic_id`、`repository_id`、`score`、`rank`、`selected_for_agent`、`manually_pinned`、`created_at`。主键为 `(topic_id, repository_id)`，索引 `(topic_id, rank)`。`manually_pinned` 标记"这条候选是人放的，不是排序器算出来的"：排序只刷新它自己算得出的行，手工/评测主题的那一条不会被下一轮日报抹掉。

## 5. Pipeline 与 Agent

### `pipeline_runs`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 运行 ID |
| `topic_id` | BIGINT UNSIGNED | FK topics.id, NULL | 主题；全局运行（`collect`/`snapshot`）为 `NULL` |
| `run_type` | VARCHAR(32) | NOT NULL | `collect`、`snapshot`、`rank`、`backfill`、`agent`、`publish`、`push` |
| `status` | VARCHAR(32) | NOT NULL | `queued/running/succeeded/failed/partial` |
| `started_at` | DATETIME(6) | NULL | 开始时间 |
| `finished_at` | DATETIME(6) | NULL | 结束时间 |
| `attempt` | INT | NOT NULL DEFAULT 1 | 第几次尝试，重试时递增 |
| `metrics_json` | JSON | NULL | 计数、业务日期和耗时等 |
| `error_code` | VARCHAR(64) | NULL | 标准错误码 |
| `error_message` | TEXT | NULL | 脱敏后的错误信息 |

索引：`(topic_id, run_type, status)`、`(started_at)`。

约束：`agent`、`publish` 和 `push` 运行必须绑定 `topic_id`；`collect`、`snapshot`、`rank`、`backfill` 可以是全局运行。
每次尝试写入独立行，因此一次重试会留下两行同一业务日期的运行记录，便于还原重试轨迹。
`backfill` 运行的 `metrics_json.backfill_job_id` 把它挂回它所属的 `backfill_jobs` 行。

### `backfill_jobs`

历史回填作业的**检查点**。续传需要的一切都在这一行的列里，而不在走查的进程里 —— 因为会死掉的正是进程。这张表没有外键：作业先于运行存在，运行通过 `pipeline_runs.metrics_json.backfill_job_id` 反向引用它。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 作业 ID |
| `provider` | VARCHAR(32) | NOT NULL | `github` / `huggingface`；一次走查只覆盖一个源 |
| `query_json` | JSON | NULL | 走查的检索参数，取自 adapter 的 `describe_query()` |
| `status` | VARCHAR(32) | NOT NULL | `pending/running/succeeded/failed/cancelled` |
| `next_page` | INT | NOT NULL DEFAULT 1 | 下一页要请求的页码，即游标 |
| `target_pages` | INT | NOT NULL | 页预算，`1..100` |
| `per_page` | INT | NOT NULL | 每页条目数，`1..100` |
| `pages_completed` | INT | NOT NULL DEFAULT 0 | 已提交的页数 |
| `items_collected` | INT | NOT NULL DEFAULT 0 | 已入库的条目数 |
| `attempt` | INT | NOT NULL DEFAULT 1 | 第几次走查，从 1 起 |
| `started_at` | DATETIME(6) | NULL | 首次被认领的时刻 |
| `finished_at` | DATETIME(6) | NULL | 收口时刻 |
| `heartbeat_at` | DATETIME(6) | NULL | 最后一次提交页的时刻，并发守卫据此判断走查者是否还活着 |
| `error_code` | VARCHAR(64) | NULL | 标准错误码 |
| `error_message` | TEXT | NULL | 脱敏后的错误信息 |
| `created_at` / `updated_at` | DATETIME(6) | NOT NULL | 由 `TimestampMixin` 维护 |

索引：`(provider, status)`、`(created_at)`。

约束：`status` 取五个合法值；`next_page >= 1`；`target_pages` 与 `per_page` 均在 `1..100`；`pages_completed` 与 `items_collected` 非负；`attempt >= 1`；`finished_at` 不早于 `started_at`。

`heartbeat_at` 是独立列而不是复用 `updated_at`：`TimestampMixin.updated_at` 的 Python 侧 `onupdate` 由 SQLAlchemy 在每次 UPDATE 时施加并**覆盖显式赋值**，所以它永远持有墙钟值，任何以它为心跳的守卫都会一直看到"走查者还活着"。一条无法被测试控制的时钟的守卫，是一条没有人会去测的守卫。

`running` 且心跳未超过 `STALE_AFTER = 15` 分钟的作业不可被第二个走查者认领。

### `agent_configs`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 配置 ID |
| `name` | VARCHAR(128) | NOT NULL | 配置名 |
| `version` | VARCHAR(32) | NOT NULL | 版本 |
| `provider` | VARCHAR(64) | NOT NULL | 模型供应商 |
| `model` | VARCHAR(128) | NOT NULL | 模型名 |
| `prompt_version` | VARCHAR(64) | NOT NULL | Prompt 版本 |
| `tool_policy_json` | JSON | NOT NULL | 工具白名单和限制 |
| `max_loops` | INT | NOT NULL | 最大循环 |
| `max_model_calls` | INT | NOT NULL | 最大模型调用 |
| `max_tool_calls` | INT | NOT NULL | 最大工具调用 |
| `max_tokens` | INT | NOT NULL DEFAULT 12000 | 单次运行最大 Token |
| `max_cost` | DECIMAL(10,6) | NOT NULL | 最大费用 |
| `enabled` | TINYINT(1) | NOT NULL | 是否启用 |
| `created_at` | DATETIME(6) | NOT NULL | UTC |

唯一约束：`UNIQUE(name, version)`。

### `agent_runs`

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | BIGINT UNSIGNED | PK |
| `pipeline_run_id` | BIGINT UNSIGNED | FK pipeline_runs.id |
| `topic_id` | BIGINT UNSIGNED | FK topics.id |
| `agent_config_id` | BIGINT UNSIGNED | FK agent_configs.id |
| `status` | VARCHAR(32) | NOT NULL |
| `window_start` | DATETIME(6) | NOT NULL |
| `window_end` | DATETIME(6) | NOT NULL |
| `loop_count` | INT | NOT NULL DEFAULT 0 |
| `tool_call_count` | INT | NOT NULL DEFAULT 0 |
| `model_call_count` | INT | NOT NULL DEFAULT 0 |
| `prompt_tokens` | INT | NOT NULL DEFAULT 0 |
| `completion_tokens` | INT | NOT NULL DEFAULT 0 |
| `estimated_cost` | DECIMAL(10,6) | NOT NULL DEFAULT 0 |
| `publish_mode` | VARCHAR(32) | NULL |
| `decision_reason_json` | JSON | NULL |
| `started_at` | DATETIME(6) | NULL |
| `finished_at` | DATETIME(6) | NULL |
| `error_code` | VARCHAR(64) | NULL |

索引：`(topic_id, status)`、`(started_at)`、`(agent_config_id)`。

### `agent_steps`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 步骤 ID |
| `agent_run_id` | BIGINT UNSIGNED | FK agent_runs.id | 运行 ID |
| `step_index` | INT | NOT NULL | 顺序 |
| `step_type` | VARCHAR(32) | NOT NULL | `plan/tool/observe/verify/write` |
| `status` | VARCHAR(32) | NOT NULL | 状态 |
| `summary` | TEXT | NULL | 脱敏摘要 |
| `input_digest` | VARCHAR(64) | NULL | 输入摘要，不存敏感原文 |
| `output_digest` | VARCHAR(64) | NULL | 输出摘要 |
| `latency_ms` | INT | NULL | 耗时 |
| `created_at` | DATETIME(6) | NOT NULL | UTC |

唯一约束：`UNIQUE(agent_run_id, step_index)`。

### `agent_run_reviews`

T29 新增。人对一条 `needs_review` 运行的判断。这类运行跑完了却没有产出报告（最常见是 `AGENT_EVIDENCE_INCOMPLETE`），所以没有「通过」这个动作可做；本表记录的是**读过的结论**，让下一个人知道这条已被看过、以及看的人怎么判。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 复核 ID |
| `agent_run_id` | BIGINT UNSIGNED | FK agent_runs.id, CASCADE | 被复核的运行 |
| `reviewer_id` | BIGINT UNSIGNED | FK users.id, SET NULL | 复核人 |
| `verdict` | VARCHAR(32) | NOT NULL | `acknowledged`（原因已确认）/ `needs_rerun`（补完数据应重跑） |
| `comment` | TEXT | NULL | 看到了什么、打算改什么 |
| `created_at` | DATETIME(6) | NOT NULL | UTC |

索引：`(agent_run_id, created_at)`。CHECK：`verdict IN ('acknowledged','needs_rerun')`。

**只增不改**：同一条运行可以有多行，改主意就再记一条，旧结论不被覆盖。**且不改 `agent_runs.status`** —— 那条运行确实没有报告，改成别的状态就等于声称有一份不存在的草稿；写入前服务判定运行存在且状态为 `needs_review`，其余状态（含 `completed`）一律拒绝，已完成运行的人工决定落在它产出的报告上（`report_reviews`）。

### `tool_calls`

字段：`id`、`agent_run_id`、`agent_step_id`、`tool_name`、`arguments_json`、`result_summary`、`status`、`error_code`、`latency_ms`、`created_at`。索引：`(agent_run_id)`、`(tool_name, status)`。

## 6. 报告、引用与审核

### `reports`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 报告 ID |
| `agent_run_id` | BIGINT UNSIGNED | FK agent_runs.id | 生成运行 |
| `topic_id` | BIGINT UNSIGNED | FK topics.id | 主题 |
| `owner_id` | BIGINT UNSIGNED | FK users.id, SET NULL, NULL | 报告负责人，`PRIVATE` 可见范围的唯一抓手 |
| `title` | VARCHAR(255) | NOT NULL | 标题 |
| `current_version` | INT | NOT NULL DEFAULT 1 | 当前版本 |
| `content_markdown` | MEDIUMTEXT | NOT NULL | 当前内容 |
| `status` | VARCHAR(32) | NOT NULL | 状态机状态 |
| `visibility` | VARCHAR(16) | NOT NULL | `PRIVATE/TEAM/INTERNAL` |
| `sensitivity` | VARCHAR(16) | NOT NULL | `PUBLIC/INTERNAL/CONFIDENTIAL` |
| `publish_mode` | VARCHAR(32) | NOT NULL | `AUTO/AUTO_WITH_DELAY/MANUAL` |
| `publish_at` | DATETIME(6) | NULL | 自动发布窗口 |
| `generated_at` | DATETIME(6) | NOT NULL | 生成时间 |
| `published_at` | DATETIME(6) | NULL | 发布时间 |
| `approved_by` | BIGINT UNSIGNED | NULL | 审核人 |

索引：`(topic_id, status)`、`(publish_at)`、`(visibility, sensitivity)`、`(owner_id, status)`。

报告状态机：`draft -> pending_review -> approved -> scheduled -> published`，失败分支为 `failed`（终态），退回分支为 `changes_requested`；T12 新增 `published -> draft`，用于撤回或回退已发布的报告。撤回与回退都以"追加新版本"实现。

`owner_id` 为 `NULL` 的 `PRIVATE` 报告只有 `ADMIN` 可读——这是"没有负责人的私有内容按最窄范围处理"的安全默认值，而不是"人人可读"。

### `report_share_links`

T26 新增。**免登录只读**的分享凭证。它存在的理由：`sensitivity='PUBLIC'` 此前只是一个标签，既不能让组织外的人读到报告，也不参与任何判定——报告的唯一出口是"给人建个账号"。这张表把"公开"变成一个可撤销的动作。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 链接 ID |
| `report_id` | BIGINT UNSIGNED | FK reports.id, CASCADE | 被分享的报告 |
| `token_hash` | CHAR(64) | NOT NULL, UNIQUE | `sha256(token)` 的十六进制；**明文不入库** |
| `created_by` | BIGINT UNSIGNED | FK users.id, SET NULL, NULL | 生成者；账号删除后链接仍在，便于追查"曾经谁分享过" |
| `expires_at` | DATETIME(6) | NULL | `NULL` 表示不过期 |
| `revoked_at` | DATETIME(6) | NULL | 撤销时刻；非空即失效 |
| `last_accessed_at` | DATETIME(6) | NULL | 最近一次成功解析 |
| `access_count` | INT | NOT NULL DEFAULT 0 | 成功解析次数 |
| `created_at` | DATETIME(6) | NOT NULL | UTC |
| `updated_at` | DATETIME(6) | NOT NULL | UTC |

索引：`UNIQUE(token_hash)`、`(report_id, revoked_at)`。

三条约束落在服务层而不是 CHECK：报告必须 `sensitivity='PUBLIC'` 且 `status='published'` 才能生成；`expires_at` 为空或晚于当前时刻；`revoked_at` 为空。**失效时一律 `404`**，不区分"不存在 / 已撤销 / 已过期 / 报告被撤回"，因为可区分就等于提供了探测接口。

`token_hash` 是这张表的安全边界：整表泄露时泄露的是"某份报告曾被分享过"，不是"现在谁都能读到哪份报告"。明文只在 `create_share_link` 的返回值里出现一次，列表接口永远不返回。

### `report_versions`

字段：`id`、`report_id`、`version`、`content_markdown`、`created_by`、`change_reason`、`created_at`。唯一约束：`UNIQUE(report_id, version)`。

T10 已实现该表：相同草稿内容重复生成不新增版本；章节内容变化时 `reports.current_version` 递增，并写入对应不可变版本。

T12 扩展了写入路径：人工编辑正文与回退历史版本都**追加**新版本（`change_reason` 分别为 `manual_edit` 与 `rollback_to_v<n>`），并同时复制被继承版本的引用集合，不覆盖任何历史行。

### `report_sources`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 来源 ID |
| `report_id` | BIGINT UNSIGNED | FK reports.id | 报告 |
| `version` | INT | NOT NULL DEFAULT 1, CHECK >= 1 | 该引用所属的报告版本 |
| `repository_id` | BIGINT UNSIGNED | NULL | 可选仓库 |
| `section_key` | VARCHAR(64) | NOT NULL | 报告章节 |
| `claim_text` | TEXT | NULL | 关键结论摘要 |
| `source_type` | VARCHAR(32) | NOT NULL | `metric/readme/prior_report` |
| `source_url` | VARCHAR(512) | NULL | 来源地址 |
| `source_ref` | VARCHAR(255) | NULL | 内部引用 |
| `metric_snapshot_id` | BIGINT UNSIGNED | NULL | 指标来源 |
| `created_at` | DATETIME(6) | NOT NULL | UTC |

索引：`(report_id, version, section_key)`、`(repository_id)`。

T12 新增 `version`，引用集合因此**按版本归属**：回退到历史版本时能恢复该版本自己的证据，`INV-10` 也总能对正在发布的版本校验。验证器、合成器与 `search_prior_reports` 的引用读写一律带 `version == reports.current_version` 限定。

### `report_drafts`

T7 新增。承载 `save_report_section` 契约中的 `report_draft_id`：Agent 在报告定稿前把章节写入草稿，草稿与最终 `reports` 分离，避免未定稿内容提前进入发布流程。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 草稿 ID |
| `agent_run_id` | BIGINT UNSIGNED | FK agent_runs.id, CASCADE | 生成运行 |
| `topic_id` | BIGINT UNSIGNED | FK topics.id, RESTRICT | 主题 |
| `status` | VARCHAR(32) | NOT NULL | `draft`、`superseded`、`promoted` |
| `created_at` | DATETIME(6) | NOT NULL | UTC |
| `updated_at` | DATETIME(6) | NOT NULL | UTC |

唯一约束：`UNIQUE(agent_run_id)`，即每个运行只有一个草稿。

### `report_sections`

T7 新增。草稿内的章节版本，幂等保存与版本递增都在这里落地。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 章节 ID |
| `report_draft_id` | BIGINT UNSIGNED | FK report_drafts.id, CASCADE | 所属草稿 |
| `section_key` | VARCHAR(64) | NOT NULL | 报告模板章节 |
| `version` | INT | NOT NULL | 章节版本，从 1 递增 |
| `content_markdown` | MEDIUMTEXT | NOT NULL | 章节正文，上限 20000 字符 |
| `content_digest` | VARCHAR(64) | NOT NULL | 内容摘要，用于幂等判定 |
| `citations_json` | JSON | NULL | 引用列表 |
| `created_at` | DATETIME(6) | NOT NULL | UTC |
| `updated_at` | DATETIME(6) | NOT NULL | UTC |

唯一约束：`UNIQUE(report_draft_id, section_key, version)`。索引：`(report_draft_id, section_key, content_digest)` 供幂等查询。CHECK：`version >= 1`。

幂等语义：`(report_draft_id, section_key, content_digest)` 已存在时返回原有版本而不新增行；内容变化时 `version` 取该章节当前最大值加一。

### `report_verifications`

T11 新增。Verifier 对某个报告版本的一次结论，是 `INV-5`「发布前必须有通过的验证结论」的可查询证据。表结构逐字段见下。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 验证 ID |
| `report_id` | BIGINT UNSIGNED | FK reports.id, CASCADE | 被验证报告 |
| `version` | INT | NOT NULL | 被验证的 `reports.current_version` |
| `verdict` | VARCHAR(16) | NOT NULL | `passed`、`failed` |
| `structure_ok` | BOOLEAN | NOT NULL | 六个固定章节与标题均存在 |
| `citations_ok` | BOOLEAN | NOT NULL | 每节至少一条引用，且所有引用可解析 |
| `numbers_ok` | BOOLEAN | NOT NULL | 含数字的章节至少一条 `metric` 引用 |
| `sensitivity_ok` | BOOLEAN | NOT NULL | 无凭据形态内容，且敏感级别与可见范围一致 |
| `reason_codes_json` | JSON | NULL | 命中的原因码列表 |
| `details_json` | JSON | NULL | 缺失标题、未解析引用、覆盖率等明细 |
| `created_at` | DATETIME(6) | NOT NULL | UTC |

唯一约束：`UNIQUE(report_id, version)`，同一版本只保留一条结论（重复验证刷新该行，不新增）。索引：`(report_id, created_at)`。CHECK：`verdict IN ('passed','failed')`、`version >= 1`。

四个布尔字段与 `A-004` 的「结构、数字、引用、敏感内容」四项检查一一对应，便于运维不解析 JSON 就能看出是哪一项拦住了发布。

### `report_reviews`

T11 落地。人对某个报告版本的审核决定，是 `MANUAL` 报告能否发布的第二个前置条件。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 审核 ID |
| `report_id` | BIGINT UNSIGNED | FK reports.id, CASCADE | 被审核报告 |
| `version` | INT | NOT NULL | 被审核的 `reports.current_version` |
| `reviewer_id` | BIGINT UNSIGNED | FK users.id, SET NULL | 审核人 |
| `decision` | VARCHAR(32) | NOT NULL | `approve`、`reject`、`request_changes` |
| `comment` | TEXT | NULL | 审核意见 |
| `edited_version_id` | BIGINT UNSIGNED | FK report_versions.id, SET NULL | 审核时基于的版本 |
| `created_at` | DATETIME(6) | NOT NULL | UTC |

索引：`(report_id, created_at)`。CHECK：`decision IN ('approve','reject','request_changes')`、`version >= 1`。

`approve` 把报告置为 `approved` 并使其可以进入发布队列；`request_changes` 置为 `changes_requested` 退回重写；`reject` 置为 `failed`，此后不能再发布。

### `push_targets`

T13 新增。报告出站的**允许列表**：除了落在本表里的目标，没有任何办法把一份报告发出去。目标默认是保守的 —— 两个 `allow_*` 开关都是 `false`，新目标不会自动获得投递 `PRIVATE` / `CONFIDENTIAL` 报告的权限。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 目标 ID |
| `name` | VARCHAR(128) | UNIQUE, NOT NULL | 目标名，唯一 |
| `channel` | VARCHAR(16) | NOT NULL | `email`、`webhook` |
| `destination` | VARCHAR(512) | NOT NULL | 邮箱地址或 HTTPS 回调地址 |
| `description` | VARCHAR(255) | NULL | 备注 |
| `enabled` | BOOLEAN | NOT NULL, default true | 停用开关 |
| `allow_private` | BOOLEAN | NOT NULL, default false | 是否允许投递 `PRIVATE` 报告 |
| `allow_confidential` | BOOLEAN | NOT NULL, default false | 是否允许投递 `CONFIDENTIAL` 报告 |
| `created_at` | DATETIME(6) | NOT NULL | UTC |
| `updated_at` | DATETIME(6) | NOT NULL | UTC |

索引：`(channel, enabled)`。CHECK：`channel IN ('email','webhook')`。

### `report_pushes`

T13 新增。一次投递尝试的记录（不论成败），同时也是重试扫描的工作队列。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 投递 ID |
| `report_id` | BIGINT UNSIGNED | FK reports.id, CASCADE | 被投递报告 |
| `target_id` | BIGINT UNSIGNED | FK push_targets.id, **RESTRICT** | 投递目标；删除目标前必须先处理记录 |
| `version` | INT | NOT NULL | 被投递的 `reports.current_version` |
| `channel` | VARCHAR(16) | NOT NULL | 反规范化的渠道，便于目标变更后追溯 |
| `destination` | VARCHAR(512) | NOT NULL | 反规范化的地址，同上 |
| `target_name` | VARCHAR(128) | NOT NULL, default `''` | 发送时的目标名；同上（T30 补，见下） |
| `status` | VARCHAR(16) | NOT NULL | `pending`、`sent`、`failed` |
| `attempt` | INT | NOT NULL, default 1 | 已尝试次数，重试只推进该列 |
| `idempotency_key` | VARCHAR(255) | UNIQUE, NOT NULL | `report:{report_id}:v{version}:target:{target_id}` |
| `error_code` | VARCHAR(64) | NULL | 失败时的稳定错误码 |
| `error_message` | VARCHAR(512) | NULL | 脱敏截断后的失败详情 |
| `message_json` | JSON | NULL | 实际发出的载荷摘要 |
| `response_excerpt` | VARCHAR(512) | NULL | 传输层返回摘要 |
| `pipeline_run_id` | BIGINT UNSIGNED | FK pipeline_runs.id, SET NULL | 本次投递的运行记录 |
| `sent_at` | DATETIME(6) | NULL | 送达时刻；未送达为 `NULL` |
| `created_at` | DATETIME(6) | NOT NULL | UTC |
| `updated_at` | DATETIME(6) | NOT NULL | UTC |

唯一约束：`UNIQUE(idempotency_key)`，同一 `(report, version, target)` 只有一行（重复请求重放该行，不重复发送）。索引：`(report_id, created_at)`、`(status, updated_at)`（后者服务于重试扫描）。CHECK：`version >= 1`、`attempt >= 0`、`status IN ('pending','sent','failed')`。

`channel` 与 `destination` 刻意反规范化：目标行之后可能被改名或改地址，但历史投递记录必须能回答"当时发到了哪里、走的哪条通道"。

`target_name`（T30 补）是同一类字段，补上它是因为前两个字段只解决了"发到哪"，没解决"发给谁"：台账曾用 `target_id` 反查 allowlist 拿名字，而 allowlist 只有 `OPS_CONFIG_ROLES` 能读 —— 于是 `EDITOR` 看到的每一行都是「（目标 #N 已不存在）」，目标其实活着且启用中。审计行因为**读者角色**变成墓碑，就不算审计行了。

## 7. 评测与反馈

四张 `eval_*` 表各回答一个问题：`eval_datasets` 是冻结数据集的**身份**，`eval_cases` 是一个样本，`eval_runs` 是"某数据集 × 某变体"的**一次执行**，`eval_results` 是该运行内**一个样本的测量**。

### `eval_datasets`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 数据集版本 ID |
| `name` | VARCHAR(128) | NOT NULL | 数据集名 |
| `version` | VARCHAR(32) | NOT NULL | 版本号 |
| `description` | VARCHAR(255) | NULL | 说明 |
| `created_by` | BIGINT UNSIGNED | FK `users.id` `ON DELETE SET NULL`，NULL | 导入者 |

唯一约束：`UNIQUE(name, version)`。索引：`(name, created_at)`。

### `eval_cases`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 样本 ID |
| `dataset_id` | BIGINT UNSIGNED | FK `eval_datasets.id` `ON DELETE CASCADE` | 所属数据集版本 |
| `case_key` | VARCHAR(64) | NOT NULL | 样本键 |
| `input_json` | JSON | NOT NULL | 输入快照（主题、窗口、标签、备注） |
| `reference_json` | JSON | NULL | 人工参考结论与 `facts` |
| `rubric_json` | JSON | NULL | 评分规则（阈值） |
| `enabled` | TINYINT(1) | NOT NULL | 是否启用 |

唯一约束：`UNIQUE(dataset_id, case_key)`。索引：`(dataset_id, enabled)`。

`enabled=false` 的样本被 runner **跳过**而不是记为 0 分，因此可以临时停用样本而不污染历史均值的分母。

### `eval_runs`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 运行 ID |
| `dataset_id` | BIGINT UNSIGNED | FK `eval_datasets.id` `ON DELETE CASCADE` | 数据集版本 |
| `target_type` | VARCHAR(32) | NOT NULL，CHECK | 变体 |
| `agent_config_id` | BIGINT UNSIGNED | FK `agent_configs.id` `ON DELETE SET NULL`，NULL | Agent 配置（baseline 为 NULL） |
| `status` | VARCHAR(16) | NOT NULL | `pending/running/succeeded/failed` |
| `started_at` / `finished_at` | DATETIME(6) | NULL | UTC；CHECK `finished_at >= started_at` |
| `summary_json` | JSON | NULL | 该变体的聚合摘要（统一对比报告的一行） |
| `error_message` | VARCHAR(512) | NULL | 运行级错误 |

约束：`target_type` 只允许 `baseline_single_prompt` / `agent_full` / `agent_no_verifier` / `agent_no_tools` / `agent_no_history` 五个取值（数据库 CHECK，与 `evaluation/targets.py:TARGET_ORDER` 逐字一致）。

索引：`(dataset_id, target_type)`、`(status, created_at)`。

`summary_json` 让运维**读一行就拿到一个变体的全貌**，不必重新聚合数百条结果行。它是 `TargetSummary.to_dict()` 的落盘形态。

### `eval_results`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 测量 ID |
| `eval_run_id` | BIGINT UNSIGNED | FK `eval_runs.id` `ON DELETE CASCADE` | 所属运行 |
| `eval_case_id` | BIGINT UNSIGNED | FK `eval_cases.id` `ON DELETE CASCADE` | 被测量的样本 |
| `status` | VARCHAR(16) | NOT NULL | `succeeded/failed` |
| `citation_coverage` | NUMERIC(5,4) | NULL | 门禁指标 |
| `factual_consistency` | NUMERIC(5,4) | NULL | 门禁指标 |
| `task_completion` | NUMERIC(5,4) | NULL | 门禁指标 |
| `cost` | NUMERIC(10,6) | NOT NULL，CHECK `>= 0` | 该样本成本 |
| `latency_ms` | INT | NULL，CHECK `>= 0` | 该样本耗时 |
| `human_score` | INT | NULL，CHECK `1..5` | 人工评分 |
| `details_json` | JSON | NULL | 其余指标与运行级证据 |

唯一约束：`UNIQUE(eval_run_id, eval_case_id)`。索引：`(eval_run_id, status)`。

`details_json` 承载 `ScoredCase.to_dict()`，其中包含**不落列**的比率（`source_accuracy`、`tool_success_rate`、`prompt_injection_block_rate`）与运行级信号（`tool_call_total`、`tool_call_denied`、`verification_sufficient_rate`）。把这些留在 JSON 里意味着新增一个被测量的比率不需要迁移 —— 只有能阻断发布的指标才值得占列。

三个门禁指标存为 `NUMERIC(5,4)`：它们都是 `0.0`–`1.0` 的比率，四位小数与评分器的量化位数一致，因此"评分得到的值"就是"落库的值"。

### `feedback`

> **计划内、尚未实现。** `feedback` 没有对应的 ORM 模型，也没有迁移；`Base.metadata` 里没有这张表。它留在这里是为了给评测的坏例回流留一个明确的目的地（`is_bad_case` 就是回流开关），而不是因为在运行中的系统里能找到它。这一行必须自己说出来：检查脚本 `scripts/check_docs_drift.py` 会把带此标注的表记入"计划"，其余"文档里有、代码里没有"的表一律判为漂移 —— 一张像普通表一样被列出来的表，读者没有义务去分辨它到底存不存在。

字段：`id`、`agent_run_id`、`report_id`、`user_id`、`feedback_type`、`label`、`comment`、`is_bad_case`、`created_at`。索引：`(is_bad_case, created_at)`。

## 8. 审计日志

### `audit_logs`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 审计 ID |
| `actor_id` | BIGINT UNSIGNED | NULL | 用户 ID |
| `action` | VARCHAR(64) | NOT NULL | 操作 |
| `resource_type` | VARCHAR(64) | NOT NULL | 资源类型 |
| `resource_id` | VARCHAR(64) | NULL | 资源 ID |
| `result` | VARCHAR(16) | NOT NULL | `success/denied/failed` |
| `request_id` | VARCHAR(64) | NULL | 请求追踪 |
| `ip_address` | VARBINARY(16) | NULL | 不展示明文 |
| `metadata_json` | JSON | NULL | 脱敏上下文 |
| `created_at` | DATETIME(6) | NOT NULL | UTC |

索引：`(actor_id, created_at)`、`(resource_type, resource_id)`、`(action, created_at)`。审计日志不提供 UPDATE/DELETE API。

## 9. 关键约束与不变量

- 同一 `(source_id, provider_repo_id)` 只能存在一个仓库。
- 同一 `(repository_id, snapshot_date)` 只能存在一个快照。
- `agent_steps.step_index` 在同一运行内唯一且递增。
- `MANUAL` 报告没有 `approve` 审核记录时不能发布。
- 报告发布前必须存在针对其 `current_version` 的 `report_verifications` 行且 `verdict=passed`；`report_verifications` 按 `(report_id, version)` 唯一，重复验证刷新而不新增。
- 发布策略只能收紧、不能放宽：`topics.publish_mode` 是上限，具体发现（验证失败、敏感内容、首次配置、运行不干净等）只会让决策更严格。
- `publish_due_reports` 只能发布处于 `scheduled` 且 `publish_at` 已到期的报告；`approved` 报告视为已获明确同意，在同一轮扫描中直接排队并发布。
- 发布链路写入的 `pipeline_runs` 若由调用方指定了时刻，`started_at` 与 `finished_at` 均使用该时刻，以满足 `finished_at >= started_at`。
- `report_sources.report_id` 必须与报告所属主题一致。
- `audit_logs` 的 `result=denied` 必须保留原始 `request_id`。
- 用户状态、数据范围、角色代码、审计结果、主题发布模式和 Pipeline 状态由数据库 CHECK 约束限制为文档定义的合法值。
- Pipeline 运行的 `agent`、`publish` 和 `push` 类型必须绑定主题；`collect`、`snapshot`、`rank`、`backfill` 允许全局运行。
- `backfill_jobs` 的游标只在页被提交时前进：每页的仓库与该页之后的新游标在同**一事务**中提交（`ingest_items` 刻意不提交，`advance_job` 提交），因此游标动过就一定有对应的行，崩溃窗口是"无害"而不只是"可容忍"。入库幂等落在 `(source_id, provider_repo_id)` 上，所以重放走查不会重复任何一行。
- `backfill_jobs.per_page` 与 `target_pages` 都被限制在 `1..100`：超出上限的页预算只会买到源不会提供的请求，而把 `next_page` 收在同一量级内也能保证它永远不会指到一个没有任何 provider 会应答的页。
- `backfill_jobs.heartbeat_at` 的语义是"最后一次提交页的时刻"，并且在**未超过** `STALE_AFTER`（15 分钟）时阻止第二个走查者认领。两个走查者共用一个游标不是重复劳动那么简单：各自都当自己是唯一的，于是同一批页被请求两次却只计数一次，预算花在对方已经走过的页上。
- 仓库 star/fork、快照 star/fork/open issues、主题 star/window/candidate 阈值、候选 score/rank 和 Pipeline attempt 由数据库 CHECK 约束保证非负或正数范围。
- `agent_configs.max_tokens` 必须为正数；`agent_runs.prompt_tokens` 与 `completion_tokens` 必须非负。
- 审计日志、仓库快照和主题候选关系的 `created_at` 由 ORM 使用 UTC 时间自动填充；业务明确需要时可显式覆盖。
- Agent 配置唯一键为 `(name, version)`；`agent_runs` 必须绑定 `pipeline_run_id`、`topic_id` 和 `agent_config_id`。
- 每个 `agent_run` 最多一个 `report_drafts` 行；`report_sections` 在草稿内按 `(report_draft_id, section_key, version)` 唯一，并可用 `(report_draft_id, section_key, content_digest)` 做幂等判定。
- `repository_contents` 唯一键为 `(repository_id, content_type)`，`content_type` 当前仅允许 `readme`。
- `tool_calls` 必须关联 `agent_run_id`，只保存脱敏且截断后的 `arguments_json` 与 `result_summary`。
- 五个 Agent 工具中只有 `save_report_section` 产生领域写入，且只写当前运行草稿；其余四个只读工具不得写入任何领域表（由注册表的只读写保护强制）。
- 报告出站只能走 `push_targets` 允许列表；目标必须 `enabled`、渠道与请求一致，并被显式开过 `allow_private` / `allow_confidential`（默认关闭）。
- 只有 `approved` / `published` 状态的报告可被推送；`CONFIDENTIAL` 报告额外要求存在针对 `current_version` 的 `approve` 审核记录。
- 同一 `(report_id, version, target_id)` 只有一条 `report_pushes`；重复请求重放该行，重试只推进 `attempt` 而不新增行。
- 同一 `(name, version)` 只有一条 `eval_datasets`：**数据集版本不可覆盖**。同名同版本但内容不同的导入被拒（`DATASET_VERSION_CONFLICT`），否则任何历史结果都不可复现。内容等价由规范化后的 SHA-256 指纹判定，重复导入只重放不新增。
- 同一 `(dataset_id, case_key)` 只有一条 `eval_cases`；同一 `(eval_run_id, eval_case_id)` 只有一条 `eval_results`，重跑刷新测量而不追加行。
- `eval_runs.target_type` 只允许五个变体取值；`eval_runs.status` 只允许 `pending/running/succeeded/failed`；`finished_at >= started_at`。
- `eval_runs` 中只有 `baseline_single_prompt` 可以 `agent_config_id IS NULL`；四个 Agent 变体必须绑定一个配置。
- 一个变体的"是否通过"由 `scored_case_count > 0` 与"无门禁失败且无回归"共同决定：**没有测量的变体不能通过**，因为全部样本失败产生的空 `gate_failures` 与"真的都达标"在数据上同形。

## 10. 写入方（T8）

T8 未新增表，但下列表的写入方自此明确：

| 表 | 写入方 | 说明 |
|---|---|---|
| `agent_configs` | `agent/runtime.py:load_or_create_config()` | 首次运行时创建 `default-research-agent`（version `v1`），后续复用 |
| `agent_runs` | `StepRecorder.mark_running()` / `sync_counters()` / `finish()` | 运行状态、四档计数（loop / tool / model / cost）与终态 |
| `agent_steps` | `StepRecorder.record()` | 每次节点执行写一行，`step_index` 在运行内递增；只存脱敏摘要与 SHA-256 摘要值，不存模型输出或 README 原文 |
| `tool_calls` | `ToolRegistry.invoke()` | 每次工具调用一行，包含被拒绝与预算拒绝的分支 |
| `pipeline_runs` | `agent/runtime.py`（`run_type=agent`） | `completed` 映射为 `succeeded`，`needs_review` 与 `failed` 映射为 `failed`，并写入 `metrics_json` |
| `report_drafts` / `report_sections` | `save_report_section` | 每个 `agent_run` 一个草稿，每个草稿 6 个章节 |

`reports` 与 `report_sources` 在 T8 仍为空：Write 只到草稿章节，报告合成与引用有效性校验属 T10。

## 11. 写入方（T10 / T11 / T12 / T13 / T14）

T10–T13 的写入方如下：

| 表 | 写入方 | 说明 |
|---|---|---|
| `reports` | `reports/composition.py:generate_report()` 建行；`reports/publishing.py` 推进 `status` / `publish_at` / `published_at`；`reports/revisions.py` 推进 `status` / `current_version` / `owner_id` 无关的元数据 / `visibility` / `sensitivity` | 首次生成写 `status=draft`；发布链路按状态机迁移 |
| `report_versions` | `reports/composition.py`、`reports/revisions.py` | 内容变化、人工编辑正文与回退历史版本都追加不可变版本；同内容重复生成不新增 |
| `report_sources` | `reports/composition.py`、`reports/revisions.py` | 整体替换**该版本**的引用集合，并把上一版的集合复制到新版本 |
| `report_verifications` | `reports/verification.py:verify_report()` | 按 `(report_id, version)` 刷新单行结论，并写 `report.verify` 审计事件 |
| `report_reviews` | `reports/publishing.py:record_review()` | 每次人工决定追加一行，并写 `report.review` 审计事件 |
| `pipeline_runs` | `reports/publishing.py`（`run_type=publish`） | 决策、阻塞与发布各留一条记录，`metrics_json.stage` 区分 `evaluate` 与 `publish` |
| `push_targets` | 运维播种（暂无 API 写入方） | 允许列表是人工维护的表，T13 只读不写 |
| `report_pushes` | `notifications/service.py:push_report()` | 每次投递尝试一行（幂等键唯一），重试推进同一行的 `attempt`，并写 `report.push` 审计事件 |
| `pipeline_runs` | `notifications/service.py`（`run_type=push`） | 每次投递（含自动推送）一条运行记录，`metrics_json` 记录目标、渠道与结果 |

T12 的读侧入口是 `reports/queries.py`：列表（带授权谓词与分页）、详情聚合、以及 8 个 API 端点与 `/ui` 页面共用的序列化。`api`、`web` 两层都不直接构造 SQL（`INTERFACES.md` §4）。

`publish_due_reports()` 是延迟发布与批准后发布的唯一推进入口：先把 `approved` 报告置为 `scheduled`，再发布所有已到期的 `scheduled` 报告；单条报告被策略拦截时记 `failed` 并继续处理其余报告，不中断整批。

T13 的出站链路只有一个写入口：`push_report()` 负责守卫、解析目标、投递与落库；`push_after_publish()`（挂在 `publish_report` 末尾）只是 `push_report()` 的扇出调用方，同样是唯一入口，**不允许**任何调用方绕过它直接写 `report_pushes`。失败行的重试同样回到 `push_report()`（`retry_push()` / `retry_failed_pushes()` 只是它的包装）。

T14 的写入方如下：

| 表 | 写入方 | 说明 |
|---|---|---|
| `eval_datasets` | `evaluation/dataset.py:import_dataset()` | 只在首次导入某个 `(name, version)` 时建行；重复导入走指纹比对并返回 `replayed=True`，不建行 |
| `eval_cases` | `evaluation/dataset.py:import_dataset()` | 随数据集版本一次性写入；版本内 `case_key` 唯一，不允许事后增删 |
| `eval_runs` | `evaluation/runner.py:run_evaluation()` 建行；`build_comparison()` 写回 `status` / `summary_json` / `finished_at` | **每个变体各一行**（不是每个数据集一行）；重跑是新行，历史执行不被改写 |
| `eval_results` | `evaluation/runner.py:_record_result()` | `UNIQUE(eval_run_id, eval_case_id)` 使重跑刷新自身测量而非追加行 |
| `pipeline_runs` | 无 | 评测不写 `pipeline_runs`：评测运行有独立的 `eval_runs` 生命周期，混入 Pipeline 会让"运行时状态"与"测量"两套语义互相污染 |

`run_evaluation()` 是评测执行的唯一入口，`api/routes/evaluations.py` 与 `evaluation/cli.py` 都只是它的调用方；`compare_latest()` 是只读入口（取每个变体最近一次运行重建报告），它**不执行任何变体、不写任何行**。

评测层的读侧入口是 `evaluation/dataset.py` 的 `list_datasets()` / `list_cases()` / `resolve_dataset()` 与 `evaluation/runner.py` 的 `list_runs()` / `latest_runs_per_target()`：`api` 层不直接构造 SQL，只把结果序列化为响应模型。

`evaluation/scorers.py` 是**纯只读**模块：它读 `repo_snapshots`、`repository_contents` 与 `reports` 来判断引用能否解析、事实是否一致，但不写任何领域表。

### T16 写入方（数据回填）

| 表 | 写入方 | 说明 |
|---|---|---|
| `backfill_jobs` | `backfill/jobs.py:create_job()` 建行；`claim_job()` / `advance_job()` / `finish_job()` / `cancel_job()` 推进 | 作业行**就是检查点**：`next_page` / `pages_completed` / `items_collected` / `attempt` / `heartbeat_at` 全在这张表的列里，进程崩了不丢进度 |
| `repositories` / `repo_snapshots` | `backfill/service.py:run_backfill()` → T4 的 `collectors/service.ingest_items()` | 复用 T4 的 `upsert_repository`，`(source_id, provider_repo_id)` 幂等；重复回填不新增仓库行 |
| `pipeline_runs` | `backfill/service.py:run_backfill()`（`run_type=backfill`） | **每次尝试一条运行记录**（第 1 次失败、第 2 次续传成功就是两条），`topic_id` 为 `NULL`（全局类型） |
| `audit_logs` | `backfill/service.py`（`action=backfill.run`） | 每次尝试一条（`success` / `failed`），`resource_id` 是作业 id |
| `agent_configs` / `reports` / `push_targets` | 无 | 回填只做采集与入库，不触发研究、合成、发布或推送 |

`run_backfill()` 是回填执行的唯一入口，`backfill/cli.py` 与 `workers/tasks.py:BACKFILL_TASK` 都只是它的调用方。**每页提交一次**：当页仓库（`ingest_items`，不提交）与游标推进（`advance_job`，提交）在**同一事务**里完成，所以崩溃窗口是"无害的" —— 要么整页连游标一起落盘，要么都不落盘，不会出现"仓库进去了但游标没动"（重复抓一页）或"游标动了但仓库没进去"（漏数据）。

`backfill_jobs.heartbeat_at` 是**独立列**而不是复用 `TimestampMixin.updated_at`：SQLAlchemy 2.0.34 的 Python 侧 `onupdate` 会在 UPDATE 时覆盖显式赋给 `updated_at` 的值（INSERT 时不覆盖），用它当心跳钉不住，并发守卫会失效。

回填**没有 HTTP 面**：只有 CLI（`backfill/cli.py` 的 `--action start|resume|status|list`）与 Celery 任务两条触发路径，`api` 层不注册任何回填端点（`INTERFACES.md` §3）。
