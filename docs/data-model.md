# 数据模型设计

> 数据库：MySQL 8.0+  
> ORM：SQLAlchemy 2.x  
> 迁移：Alembic  
> 最后更新：2026-10-12

## 1. 设计原则

- 可查询、可聚合、可审计的数据进入结构化列；仅变化频繁且不需要单独查询的配置进入 JSON。
- Agent 轨迹、工具调用、报告来源和审计日志必须持久化，不能只写应用日志。
- 时间统一存 UTC；`snapshot_date` 使用 `DATE`，表示采集自然日。
- 所有外部 ID 同时保存供应商和供应商内 ID，避免跨平台冲突。
- 关键查询字段建立索引；唯一业务约束不能只靠应用代码判断。

## 2. 实体关系摘要

```text
users --< user_roles >-- roles
topics --< repositories >-- repo_snapshots
topics --< pipeline_runs（collect/snapshot 为全局运行，topic_id 为 NULL）
topics --< agent_configs --< agent_runs --< agent_steps --< tool_calls
agent_runs --< reports --< report_sources
reports --< report_reviews
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

索引：`(topic_id, status)`、`(publish_at)`、`(visibility, sensitivity)`。

报告状态机：`draft -> pending_review -> approved -> scheduled -> published`，失败分支为 `failed`，退回分支为 `changes_requested`。

### `report_versions`

字段：`id`、`report_id`、`version`、`content_markdown`、`created_by`、`change_reason`、`created_at`。唯一约束：`UNIQUE(report_id, version)`。

### `report_sources`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK | 来源 ID |
| `report_id` | BIGINT UNSIGNED | FK reports.id | 报告 |
| `repository_id` | BIGINT UNSIGNED | NULL | 可选仓库 |
| `section_key` | VARCHAR(64) | NOT NULL | 报告章节 |
| `claim_text` | TEXT | NULL | 关键结论摘要 |
| `source_type` | VARCHAR(32) | NOT NULL | `metric/readme/prior_report` |
| `source_url` | VARCHAR(512) | NULL | 来源地址 |
| `source_ref` | VARCHAR(255) | NULL | 内部引用 |
| `metric_snapshot_id` | BIGINT UNSIGNED | NULL | 指标来源 |
| `created_at` | DATETIME(6) | NOT NULL | UTC |

索引：`(report_id, section_key)`、`(repository_id)`。

### `report_reviews`

字段：`id`、`report_id`、`reviewer_id`、`decision`、`comment`、`edited_version_id`、`created_at`。`decision` 取值为 `approve`、`reject`、`request_changes`。

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
- `report_sources.report_id` 必须与报告所属主题一致。
- `audit_logs` 的 `result=denied` 必须保留原始 `request_id`。
- 用户状态、数据范围、角色代码、审计结果、主题发布模式和 Pipeline 状态由数据库 CHECK 约束限制为文档定义的合法值。
- Pipeline 运行的 `agent` 和 `publish` 类型必须绑定主题；`collect`、`snapshot`、`rank` 允许全局运行。
- 仓库 star/fork、快照 star/fork/open issues、主题 star/window/candidate 阈值、候选 score/rank 和 Pipeline attempt 由数据库 CHECK 约束保证非负或正数范围。
- 审计日志、仓库快照和主题候选关系的 `created_at` 由 ORM 使用 UTC 时间自动填充；业务明确需要时可显式覆盖。
