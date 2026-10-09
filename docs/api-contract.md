# API 接口契约

> 版本：v1  
> 前缀：`/api/v1`  
> 最后更新：2026-09-24

## 1. 通用约定

- 请求与响应默认使用 `application/json; charset=utf-8`。
- 时间为 ISO 8601 UTC，例如 `2026-09-24T02:00:00Z`；前端转换为 `Asia/Shanghai`。
- 资源 ID 使用整数，除认证和公开健康检查外均要求登录。
- 列表使用 `page`、`page_size`，`page_size` 范围为 1-100，默认 20。
- 创建和更新使用 `Idempotency-Key` 可选请求头；相同 Key 的重复请求不得重复创建资源。
- 错误响应使用 RFC 7807 Problem Details。
- 常用错误码：`AUTH_REQUIRED`、`FORBIDDEN`、`NOT_FOUND`、`VALIDATION_ERROR`、`CONFLICT`、`RATE_LIMITED`、`AGENT_BUDGET_EXCEEDED`、`PUBLISH_POLICY_BLOCKED`。

### `GET /health`

权限：公开。该接口只表示进程存活，不检查 MySQL 或 Redis。

```json
{
  "status": "ok",
  "service": "open-source-intelligence-agent",
  "version": "0.1.0"
}
```

### `GET /`

权限：公开。浏览器访问根路径时返回 `307 Temporary Redirect`：

- 启用文档时跳转到 `/docs`。
- 关闭文档时跳转到 `/health`。

错误响应示例：

```json
{
  "type": "https://example.local/problems/forbidden",
  "title": "Forbidden",
  "status": 403,
  "detail": "当前用户无权读取该报告",
  "instance": "/api/v1/reports/42",
  "code": "REPORT_ACCESS_DENIED",
  "request_id": "req_01J..."
}
```

## 2. 认证与当前用户

### `POST /auth/login`

请求：

```json
{
  "username": "admin",
  "password": "secret"
}
```

响应：

```json
{
  "access_token": "jwt...",
  "token_type": "bearer",
  "expires_in": 3600,
  "user": {
    "id": 1,
    "username": "admin",
    "roles": ["ADMIN"]
  }
}
```

首期也可将 JWT 写入 `HttpOnly` Cookie 供 Web UI 使用；API 客户端使用 Bearer Token。

### `GET /auth/me`

返回当前用户的 `id`、`username`、`roles` 和允许的 `data_scope`。不得在响应中返回密码散列。

## 3. 数据源与主题

### `GET /sources`

权限：`ADMIN`、`EDITOR` 可读；分页返回。

```json
{
  "items": [{
    "id": 1,
    "name": "GitHub",
    "source_type": "github",
    "enabled": true,
    "rate_limit": 30,
    "last_success_at": "2026-09-24T02:00:00Z"
  }],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

### `POST /sources`

权限：`ADMIN`。请求可包含 `name`、`source_type`、`base_url`、`credential_ref`、`enabled`、`config_json`。API 不直接接收明文长期凭证，应接收 Secret Manager 引用或一次性写入后立即加密。

### `GET /topics`

```json
{
  "id": 7,
  "name": "AI Agent 基础设施",
  "publish_mode": "AUTO_WITH_DELAY",
  "enabled": true
}
```

### `POST /topics/{topic_id}/run`

权限：`ADMIN`、`EDITOR`。手动触发一次该主题的采集、筛选与 Agent 研究。

```json
{
  "window_start": "2026-09-17T00:00:00Z",
  "window_end": "2026-09-24T00:00:00Z",
  "force": false
}
```

响应 `202 Accepted`：

```json
{
  "pipeline_run_id": 101,
  "status": "queued"
}
```

## 4. 仓库与趋势

### `GET /repositories`

查询参数：`topic_id`、`language`、`min_stars`、`created_after`、`q`、`sort`、`page`、`page_size`。

```json
{
  "items": [{
    "id": 501,
    "provider": "github",
    "full_name": "example/agent-runtime",
    "language": "Python",
    "stars": 12400,
    "forks": 920,
    "last_seen_at": "2026-09-24T02:00:00Z"
  }],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

### `GET /repositories/{repository_id}/snapshots`

查询参数：`start`、`end`、`granularity=day`。返回 `snapshot_date`、`stars`、`forks`、`stars_delta` 和 `forks_delta`。

## 5. Agent 运行与轨迹

### `POST /agent/runs`

权限：`ADMIN`、`EDITOR`。

```json
{
  "topic_id": 7,
  "agent_config_id": 3,
  "window_start": "2026-09-17T00:00:00Z",
  "window_end": "2026-09-24T00:00:00Z",
  "candidate_repo_ids": [501, 502, 503]
}
```

响应 `202 Accepted`：

```json
{
  "agent_run_id": 9001,
  "status": "queued"
}
```

### `GET /agent/runs/{agent_run_id}`

```json
{
  "id": 9001,
  "topic_id": 7,
  "agent_config_id": 3,
  "status": "needs_review",
  "loop_count": 2,
  "tool_call_count": 5,
  "model_call_count": 3,
  "estimated_cost": 0.042,
  "publish_decision": {
    "mode": "MANUAL",
    "reason_codes": ["LOW_CITATION_COVERAGE"],
    "requires_review": true
  }
}
```

### `GET /agent/runs/{agent_run_id}/steps`

按顺序返回 `agent_steps` 和关联的 `tool_calls`。默认隐藏原文中的敏感字段，只返回摘要、耗时、状态和错误。

## 6. 报告与审核

### `GET /reports`

查询参数：`topic_id`、`status`、`visibility`、`sensitivity`、`created_after`、`page`、`page_size`。权限过滤必须在数据库查询阶段完成，不能先取全量再在应用层过滤。

```json
{
  "items": [{
    "id": 42,
    "title": "2026-W39 AI Agent 开源情报周报",
    "status": "pending_review",
    "visibility": "INTERNAL",
    "sensitivity": "INTERNAL",
    "publish_mode": "AUTO_WITH_DELAY"
  }],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

### `GET /reports/{report_id}`

返回报告版本、章节、来源和审核状态。无权访问返回 403，不返回报告元数据以降低信息泄露。

### `PATCH /reports/{report_id}`

权限：`ADMIN`、`EDITOR`。请求允许修改 `title`、`content_markdown`、`visibility`、`sensitivity` 和 `publish_at`。修改后生成新版本，旧版本不得直接覆盖。

### `POST /reports/{report_id}/review`

```json
{
  "decision": "approve",
  "comment": "来源和数字已核对",
  "edited_version_id": 12
}
```

`decision` 取值：`approve`、`reject`、`request_changes`。拒绝和退回必须记录原因，并生成 `feedback` 记录。

### `POST /reports/{report_id}/publish`

发布前检查状态、目标读者、审核记录、关键指标验证和推送目标允许列表。`MANUAL` 报告在审核通过前不得发布。

### `POST /reports/{report_id}/push`

```json
{
  "channel": "email",
  "recipients": ["team@example.com"],
  "include_attachment": true
}
```

推送成功和失败均写审计日志。失败可重试，但同一 `idempotency_key` 不得产生重复发送。

## 7. 评测

### `POST /evaluations`

```json
{
  "dataset_id": 11,
  "target": "agent",
  "agent_config_id": 3,
  "scorers": ["citation_coverage", "factual_consistency", "task_completion", "cost"]
}
```

### `GET /evaluations/{evaluation_run_id}`

```json
{
  "id": 88,
  "status": "completed",
  "metrics": {
    "citation_coverage": 0.96,
    "factual_consistency": 0.92,
    "task_completion_rate": 0.95,
    "average_cost": 0.038
  },
  "baseline_delta": {
    "citation_coverage": 0.13,
    "factual_consistency": 0.08
  }
}
```

## 8. 监控与审计

- `GET /metrics/overview`：返回最近时间窗口内的任务成功率、失败原因、Token、费用、延迟和人工干预率；仅 `ADMIN`、`EDITOR` 可读。
- `GET /audit-logs`：仅 `ADMIN` 可读，支持按操作者、动作、资源、时间过滤；不提供修改接口。

## 9. 状态码与幂等约定

| 操作 | 成功状态 | 说明 |
|---|---:|---|
| 查询 | 200 | 返回当前权限范围内的数据 |
| 创建异步任务 | 202 | 返回任务 ID，不等待完成 |
| 创建资源 | 201 | 返回资源 ID |
| 更新 | 200 | 返回新版本 |
| 非法参数 | 422 | 返回字段级错误 |
| 权限不足 | 403 | 不泄露目标资源是否存在 |
| 冲突或重复 | 409 | 唯一约束或状态机冲突 |

## 10. OpenAPI 要求

实现阶段 FastAPI 必须导出 OpenAPI 3.x 文档。Pydantic 模型是运行时契约来源，本文档是设计契约；两者不一致时先更新本文档，再修改代码，并在 `ARCHITECTURE.md` Changelog 中记录接口变更。
