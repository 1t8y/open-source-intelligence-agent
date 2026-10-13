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
topics --< pipeline_runs（collect/snapshot/rank 为全局运行，topic_id 为 NULL）
agent_configs --< agent_runs --< agent_steps --< tool_calls
pipeline_runs --< agent_runs；topics --< agent_runs
agent_runs --< report_drafts --< report_sections
agent_runs --< reports --< report_versions
users --< reports（owner_id）
reports --< report_sources（按 report_id + version 归属）
reports --< report_reviews
reports --< report_verifications
eval_datasets --< eval_cases --< eval_results >-- eval_runs
users --< feedback
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
| `source_url` | VARCHAR(512) | NOT NULL | 内容来源地址 |
| `content` | MEDIUMTEXT | NOT NULL | 清理后的正文 |
| `content_hash` | VARCHAR(64) | NOT NULL | 入库时的内容摘要 |
| `fetched_at` | DATETIME(6) | NOT NULL | 抓取时间 UTC |
| `created_at` | DATETIME(6) | NOT NULL | UTC |
| `updated_at` | DATETIME(6) | NOT NULL | UTC |

唯一约束：`UNIQUE(repository_id, content_type)`。CHECK：`content_type IN ('readme')`。

### `topic_repositories`

主题与候选仓库的关系表，字段：`topic_id`、`repository_id`、`score`、`rank`、`selected_for_agent`、`created_at`。主键为 `(topic_id, repository_id)`，索引 `(topic_id, rank)`。

## 5. Pipeline 与 Agent

### `pipeline_runs`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 运行 ID |
| `topic_id` | BIGINT UNSIGNED | FK topics.id, NULL | 主题；全局运行（`collect`/`snapshot`）为 `NULL` |
| `run_type` | VARCHAR(32) | NOT NULL | `collect`、`snapshot`、`rank`、`agent`、`publish` |
| `status` | VARCHAR(32) | NOT NULL | `queued/running/succeeded/failed/partial` |
| `started_at` | DATETIME(6) | NULL | 开始时间 |
| `finished_at` | DATETIME(6) | NULL | 结束时间 |
| `attempt` | INT | NOT NULL DEFAULT 1 | 第几次尝试，重试时递增 |
| `metrics_json` | JSON | NULL | 计数、业务日期和耗时等 |
| `error_code` | VARCHAR(64) | NULL | 标准错误码 |
| `error_message` | TEXT | NULL | 脱敏后的错误信息 |

索引：`(topic_id, run_type, status)`、`(started_at)`。

约束：`agent` 和 `publish` 运行必须绑定 `topic_id`；`collect`、`snapshot`、`rank` 可以是全局运行。
每次尝试写入独立行，因此一次重试会留下两行同一业务日期的运行记录，便于还原重试轨迹。

索引：`(topic_id, run_type, status)`、`(started_at)`。

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

## 7. 评测与反馈

### `eval_datasets`

字段：`id`、`name`、`version`、`description`、`created_by`、`created_at`。唯一约束：`UNIQUE(name, version)`。

### `eval_cases`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 样本 ID |
| `dataset_id` | BIGINT UNSIGNED | FK eval_datasets.id | 评测集 |
| `case_key` | VARCHAR(64) | NOT NULL | 样本键 |
| `input_json` | JSON | NOT NULL | 输入快照 |
| `reference_json` | JSON | NULL | 人工参考结论 |
| `rubric_json` | JSON | NULL | 评分规则 |
| `enabled` | TINYINT(1) | NOT NULL | 是否启用 |

唯一约束：`UNIQUE(dataset_id, case_key)`。

### `eval_runs`

字段：`id`、`dataset_id`、`target_type`、`agent_config_id`、`status`、`started_at`、`finished_at`、`summary_json`、`error_message`。

### `eval_results`

字段：`id`、`eval_run_id`、`eval_case_id`、`citation_coverage`、`factual_consistency`、`task_completion`、`cost`、`latency_ms`、`human_score`、`details_json`。唯一约束：`UNIQUE(eval_run_id, eval_case_id)`。

### `feedback`

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
- Pipeline 运行的 `agent` 和 `publish` 类型必须绑定主题；`collect`、`snapshot`、`rank` 允许全局运行。
- 仓库 star/fork、快照 star/fork/open issues、主题 star/window/candidate 阈值、候选 score/rank 和 Pipeline attempt 由数据库 CHECK 约束保证非负或正数范围。
- `agent_configs.max_tokens` 必须为正数；`agent_runs.prompt_tokens` 与 `completion_tokens` 必须非负。
- 审计日志、仓库快照和主题候选关系的 `created_at` 由 ORM 使用 UTC 时间自动填充；业务明确需要时可显式覆盖。
- Agent 配置唯一键为 `(name, version)`；`agent_runs` 必须绑定 `pipeline_run_id`、`topic_id` 和 `agent_config_id`。
- 每个 `agent_run` 最多一个 `report_drafts` 行；`report_sections` 在草稿内按 `(report_draft_id, section_key, version)` 唯一，并可用 `(report_draft_id, section_key, content_digest)` 做幂等判定。
- `repository_contents` 唯一键为 `(repository_id, content_type)`，`content_type` 当前仅允许 `readme`。
- `tool_calls` 必须关联 `agent_run_id`，只保存脱敏且截断后的 `arguments_json` 与 `result_summary`。
- 五个 Agent 工具中只有 `save_report_section` 产生领域写入，且只写当前运行草稿；其余四个只读工具不得写入任何领域表（由注册表的只读写保护强制）。

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

## 11. 写入方（T10 / T11 / T12）

T10–T12 的写入方如下：

| 表 | 写入方 | 说明 |
|---|---|---|
| `reports` | `reports/composition.py:generate_report()` 建行；`reports/publishing.py` 推进 `status` / `publish_at` / `published_at`；`reports/revisions.py` 推进 `status` / `current_version` / `owner_id` 无关的元数据 / `visibility` / `sensitivity` | 首次生成写 `status=draft`；发布链路按状态机迁移 |
| `report_versions` | `reports/composition.py`、`reports/revisions.py` | 内容变化、人工编辑正文与回退历史版本都追加不可变版本；同内容重复生成不新增 |
| `report_sources` | `reports/composition.py`、`reports/revisions.py` | 整体替换**该版本**的引用集合，并把上一版的集合复制到新版本 |
| `report_verifications` | `reports/verification.py:verify_report()` | 按 `(report_id, version)` 刷新单行结论，并写 `report.verify` 审计事件 |
| `report_reviews` | `reports/publishing.py:record_review()` | 每次人工决定追加一行，并写 `report.review` 审计事件 |
| `pipeline_runs` | `reports/publishing.py`（`run_type=publish`） | 决策、阻塞与发布各留一条记录，`metrics_json.stage` 区分 `evaluate` 与 `publish` |

T12 的读侧入口是 `reports/queries.py`：列表（带授权谓词与分页）、详情聚合、以及 8 个 API 端点与 `/ui` 页面共用的序列化。`api`、`web` 两层都不直接构造 SQL（`INTERFACES.md` §4）。

`publish_due_reports()` 是延迟发布与批准后发布的唯一推进入口：先把 `approved` 报告置为 `scheduled`，再发布所有已到期的 `scheduled` 报告；单条报告被策略拦截时记 `failed` 并继续处理其余报告，不中断整批。
