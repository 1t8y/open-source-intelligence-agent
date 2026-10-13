# API 接口契约

> 版本：v1.3
> 前缀：`/api/v1`  
> 最后更新：2026-10-14

## 1. 通用约定

- 请求与响应默认使用 `application/json; charset=utf-8`。
- 时间为 ISO 8601 UTC，例如 `2026-09-24T02:00:00Z`；前端转换为 `Asia/Shanghai`。
- 资源 ID 使用整数，除认证和公开健康检查外均要求登录。
- 列表使用 `page`、`page_size`，`page_size` 范围为 1-100，默认 20。
- 创建和更新使用 `Idempotency-Key` 可选请求头；相同 Key 的重复请求不得重复创建资源。
- 错误响应使用 RFC 7807 Problem Details，成员固定为 `type`、`title`、`status`、`detail`、`instance`、`code`、`request_id`。
- 已实现的错误码：`AUTH_REQUIRED`、`FORBIDDEN`、`NOT_FOUND`、`VALIDATION_ERROR`、`CONFLICT`、`METHOD_NOT_ALLOWED`、`RATE_LIMITED`、`INTERNAL_ERROR`、`SERVICE_UNAVAILABLE`、`AGENT_BUDGET_EXCEEDED`、`PUBLISH_POLICY_BLOCKED`、`REPORT_STATE_INVALID`。客户端应按 `code` 分支，不要解析 `detail` 文案。

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
  "detail": "insufficient permissions for this report",
  "instance": "/api/v1/reports/42",
  "code": "FORBIDDEN",
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
    "report_id": 42,
    "mode": "MANUAL",
    "resolution": "pending_review",
    "reason_codes": ["VERIFICATION_FAILED", "LOW_CITATION_COVERAGE"],
    "requires_review": true,
    "publish_at": null
  }
}
```

### `GET /agent/runs/{agent_run_id}/steps`

按顺序返回 `agent_steps` 和关联的 `tool_calls`。默认隐藏原文中的敏感字段，只返回摘要、耗时、状态和错误。

## 6. 报告与审核

### 访问控制

所有报告端点都先执行身份认证、RBAC 和资源 ACL。列表的权限过滤**必须在数据库查询阶段完成**（授权谓词进 `WHERE` 子句），不能先取全量再在应用层过滤，否则 `total` 和翻页位置会泄露无权报告的存在。

| 角色 / 条件 | 可读的报告 |
|---|---|
| `ADMIN` | 全部 |
| 任意登录用户 | `visibility=INTERNAL` |
| `data_scope=team` | 追加 `visibility=TEAM` |
| 报告负责人（`reports.owner_id = user.id`） | 追加本人的 `visibility=PRIVATE` |
| `ADMIN`（无负责人的 `PRIVATE`） | `owner_id IS NULL` 的 `visibility=PRIVATE` |

写操作（`PATCH`、`verify`、`review`、`publish`、`rollback`）要求 `ADMIN` 或 `EDITOR`，并且**仍然**要求对该报告有读权限。越权读取返回 `403 FORBIDDEN` 且响应体不含报告标题等元数据；越权写返回 `403 FORBIDDEN` 并写 `auth.rbac` / `denied` 审计。

### `GET /reports`

查询参数：`topic_id`、`status`、`visibility`、`sensitivity`、`generated_after`、`page`、`page_size`（1-100，默认 20）。未知枚举值返回 `422 VALIDATION_ERROR`。

```json
{
  "items": [{
    "id": 42,
    "agent_run_id": 17,
    "topic_id": 3,
    "owner_id": 9,
    "title": "2026-W39 AI Agent 开源技术情报周报",
    "current_version": 2,
    "status": "pending_review",
    "visibility": "INTERNAL",
    "sensitivity": "INTERNAL",
    "publish_mode": "AUTO_WITH_DELAY",
    "publish_at": "2026-10-13T13:00:00+00:00",
    "generated_at": "2026-10-13T12:00:00+00:00",
    "published_at": null,
    "topic_name": "AI Agent"
  }],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

`items` 按 `generated_at` 倒序；`topic_name` 由服务端补齐，避免客户端二次查询。

### `GET /reports/{report_id}`

返回报告本体、全量版本历史、**当前版本**的引用与验证结论、以及全部审核记录。无权访问返回 `403`（不返回报告元数据）；不存在返回 `404 NOT_FOUND`。

```json
{
  "report": { "id": 42, "status": "draft", "current_version": 2, "title": "..." },
  "versions": [
    { "version": 2, "change_reason": "manual_edit", "created_by": 9, "created_at": "..." },
    { "version": 1, "change_reason": null, "created_by": null, "created_at": "..." }
  ],
  "sources": [
    { "version": 2, "section_key": "summary", "source_type": "metric",
      "source_ref": "repo_snapshots:12:2026-10-11", "source_url": "https://example.com/readme" }
  ],
  "verification": {
    "version": 1, "verdict": "passed", "structure_ok": true, "citations_ok": true,
    "numbers_ok": true, "sensitivity_ok": true,
    "citation_coverage": 1.0, "reason_codes": []
  },
  "reviews": [
    { "version": 1, "decision": "approve", "reviewer_id": 9, "comment": "来源与数字已核对", "created_at": "..." }
  ]
}
```

`verification` 与 `sources` 都按 `reports.current_version` 过滤，所以它们永远描述"现在这一版"，而不是历史某一版。

### `PATCH /reports/{report_id}`

权限：`ADMIN`、`EDITOR`。请求体字段全部可选，只提交要改的字段：

```json
{
  "title": "…",
  "content_markdown": "…",
  "visibility": "PRIVATE",
  "sensitivity": "INTERNAL",
  "publish_at": "2026-10-13T13:00:00+00:00"
}
```

- `content_markdown` 变化时**追加不可变版本**（`report_versions` 新行）并复制上一版的引用集合，`reports.current_version` 递增，报告退回 `draft` 等待重新验证；旧版本不得直接覆盖。
- 只改元数据不产生新版本，`content_changed` 返回 `false`。
- 只有 `draft` 与 `changes_requested` 可编辑，其他状态返回 `409 REPORT_STATE_INVALID`。
- `visibility` 只接受 `PRIVATE` / `TEAM` / `INTERNAL`，其他值返回 `422 VALIDATION_ERROR`。

```json
{ "report_id": 42, "version": 2, "status": "draft", "content_changed": true,
  "changed_fields": ["content_markdown"], "restored_from": null }
```

### `POST /reports/{report_id}/verify`

权限：`ADMIN`、`EDITOR`。对**当前版本**重跑 `A-004` 四项检查并把结论幂等写入 `report_verifications`。

```json
{ "report_id": 42, "version": 2, "verdict": "passed", "structure_ok": true,
  "citations_ok": true, "numbers_ok": true, "sensitivity_ok": true,
  "citation_coverage": 1.0, "reason_codes": [], "missing_headings": [],
  "unresolved_refs": [], "sensitive_markers": 0 }
```

### `POST /reports/{report_id}/review`

权限：`ADMIN`、`EDITOR`。只有 `pending_review` 状态可被审核，否则 `409 REPORT_STATE_INVALID`。未知 `decision` 返回 `400 VALIDATION_ERROR`。`edited_version_id` 非空时必须指向**同一份报告**的版本行，否则 `400 VALIDATION_ERROR`（外键只能证明行存在，不能证明归属）。

```json
{ "decision": "approve", "comment": "来源和数字已核对", "edited_version_id": null }
```

`decision` 取值与目标状态：`approve -> approved`、`request_changes -> changes_requested`、`reject -> failed`（终态）。拒绝和退回必须记录原因。响应为 `{ "report_id", "version", "status", "decision", "reviewer_id", "comment" }`。

### `POST /reports/{report_id}/publish`

权限：`ADMIN`、`EDITOR`。先执行验证与策略决策，再按决策落状态。前置条件（T11 实现，T12 暴露）：

- 报告处于 `changes_requested` / `failed` 时返回 `409 REPORT_STATE_INVALID`。
- 必须存在针对 `current_version` 且 `verdict=passed` 的 `report_verifications` 行，否则 `409 PUBLISH_POLICY_BLOCKED`。
- `MANUAL` 报告在存在同一版本的 `approve` 记录前不得发布，否则 `409 PUBLISH_POLICY_BLOCKED`。
- 窗口未到期的排队报告不会被提前释放；只有当 `publish_at <= now` 时本端点才尝试真正发布，此时缺验证/缺审核会以 `409` 暴露，而不是只回一句"已排队"。

响应含 `status` 与 `publish_decision`（`resolution=scheduled` 已入队 / `resolution=pending_review` 已转人工且 `publish_at` 为空，详见 `INTERFACES.md` §8）。

### `POST /reports/{report_id}/rollback`

权限：`ADMIN`、`EDITOR`。把指定历史版本恢复为**新的当前版本**，不重写历史。

```json
{ "version": 1, "comment": "回退到上周口径" }
```

- 追加新版本（`change_reason = rollback_to_v<n>`）并从被回退版本复制引用集合，报告回到 `draft`。
- 只允许从 `draft` / `changes_requested` / `published` 发起（状态机指向 `draft` 的全部入边）；仍在下游发布路径上的报告（`pending_review` / `approved` / `scheduled`）返回 `409 REPORT_STATE_INVALID`。
- 目标为当前版本返回 `400 VALIDATION_ERROR`；版本不存在返回 `404 NOT_FOUND`。
- 状态不满足时在**任何写入之前**拒绝：报告保持原状（`current_version`、正文与引用集都不变），不做半途修改。
- 响应 `{ "report_id", "version", "restored_from", "status", "comment" }`。

### `POST /reports/publish-due`

权限：`ADMIN`、`EDITOR`。运行一次到期释放驱动，等价于 Celery 的 `publish-due-reports` 周期任务。单条报告被策略拦截时记 `failed` 并写稳定错误码，不中断整批。

```json
{ "released_count": 1, "blocked_count": 1,
  "released": [{ "report_id": 42, "status": "published", "publish_mode": "AUTO_WITH_DELAY",
                 "error_code": null, "published_at": "2026-10-13T13:00:00+00:00" }] }
```

同组的 `verify` / `review` / `rollback` 端点返回 `{ "status": "success" }` 形状的确认。

### `POST /reports/{report_id}/push`

权限：`ADMIN`、`EDITOR`，且调用者必须通过该报告的读权限（`PRIVATE` 报告需 `owner_id == user.id`）。把**当前版本**投递到一个允许列表目标。

请求体：

```json
{ "channel": "email", "target_id": 7 }
```

`channel` 取 `email` 或 `webhook`，必须与目标行的渠道一致；`target_id` 是 `push_targets.id`。

守卫顺序固定，任一违规抛 Problem Details：

| 条件 | 状态码 | `code` |
|---|---|---|
| 报告状态不是 `approved` / `published` | `409` | `PUSH_BLOCKED` |
| `CONFIDENTIAL` 报告缺少当前版本的 `approve` 审核 | `409` | `PUSH_APPROVAL_REQUIRED` |
| 目标不存在 / 已禁用 / 渠道不符 / 未对该可见范围或敏感级别开放 | `400` | `PUSH_TARGET_NOT_ALLOWED` |
| `push_delivery=live` 但缺 SMTP 主机或发件人 | `400` | `PUSH_TARGET_NOT_ALLOWED` |
| 推送能力被关闭 | `503` | `PUSH_DISABLED` |
| 未登录 / 越权 | `401` / `403` | `AUTH_REQUIRED` / `FORBIDDEN` |

**传输失败返回 `200`**，响应体里 `status="failed"` 并带稳定 `error_code`（`PUSH_DELIVERY_FAILED`），因为它是被记录的可重试结果而不是请求错误：

```json
{ "push_id": 15, "report_id": 42, "version": 1, "channel": "email",
  "destination": "ops@example.com", "status": "sent", "attempt": 1,
  "error_code": null, "error_message": null, "delivered": true, "replayed": false }
```

幂等：同一 `(report_id, version, target_id)` 三元组只有一条记录。重复请求不重发，返回同一行并置 `replayed=true`；失败行的重试**推进同一行的 `attempt`**，`attempt` 用尽后不再重试。

### `GET /push/targets`

权限：`ADMIN`、`EDITOR`。返回启用的投递允许列表，可用 `channel=email|webhook` 过滤。

```json
{ "items": [{ "id": 7, "name": "ops-email", "channel": "email",
  "destination": "ops@example.com", "description": null, "enabled": true,
  "allow_private": false, "allow_confidential": false }] }
```

### `GET /push/records`

权限：`ADMIN`、`EDITOR`。投递记录，最新优先；可按 `report_id` 与 `status=pending|sent|failed` 过滤，`limit` 范围 1-200、默认 50。

```json
{ "items": [{ "id": 15, "report_id": 42, "version": 1, "channel": "email",
  "destination": "ops@example.com", "status": "sent", "attempt": 1,
  "error_code": null, "error_message": null, "sent_at": "2026-10-13T13:00:00+00:00",
  "created_at": "2026-10-13T12:59:59+00:00", "updated_at": "2026-10-13T13:00:00+00:00" }],
  "status_counts": { "sent": 1 } }
```

### `POST /push/retry`

权限：`ADMIN`、`EDITOR`。运行一次失败重试扫描，等价于 Celery 的 `retry-failed-pushes` 周期任务；`limit` 范围 1-100、默认 100。单条不可重试的行被跳过并记 `denied` 审计，不中断整批。

```json
{ "attempted_count": 1, "delivered_count": 1, "failed_count": 0,
  "outcomes": [{ "push_id": 15, "report_id": 42, "version": 1, "channel": "webhook",
    "destination": "https://hooks.example.com/osia", "status": "sent", "attempt": 2,
    "error_code": null, "error_message": null, "delivered": true, "replayed": false }] }
```

### 管理界面（`/ui`）

同一套服务层的服务端渲染界面（Jinja2 + HTMX，`include_in_schema=False`）：

| 路由 | 说明 |
|---|---|
| `GET /ui/login`、`POST /ui/login`、`POST /ui/logout` | 登录表单与退出；会话存 `HttpOnly` Cookie |
| `GET /ui/reports` | 报告列表，支持 `status`、`visibility`、`page`、`page_size`，权限过滤同 API |
| `GET /ui/reports/{report_id}` | 报告详情：验证结论、引用与证据、版本历史、审核记录、编辑表单、推送卡片与投递记录 |
| `POST /ui/reports/{report_id}/{verify,review,publish,rollback,edit}` | 动作，返回刷新后的报告片段 |
| `POST /ui/reports/{report_id}/push` | 推送到一个允许列表目标，返回刷新后的报告片段；渠道取自目标行，表单传 `channel` 无效 |
| `POST /ui/reports/publish-due` | 到期释放驱动，返回释放结果片段 |

写操作必须携带与 Cookie 一致的 `csrf_token` 表单字段（双提交校验）：缺失或不匹配返回 `403`。只读角色（`VIEWER`）的写操作返回 `403`。未登录访问任意 `/ui` 页面返回 `303` 重定向到 `/ui/login?next=<原路径>`。

## 7. 评测

前缀 `/api/v1/evaluations`。**全部端点要求写角色**（`ADMIN` / `EDITOR`）：运行一次评测会用真实数据执行 Agent，属于运维动作而非读取，因此 `VIEWER` 一律 `403`，匿名 `401`。

路由声明顺序是契约的一部分：`/runs` 与 `/runs/{run_id}` 是字面量段，必须先于以路径参数开头的 `/{dataset_id}/...` 声明，否则 `/runs` 会被解析成 dataset id。

| 端点 | 成功 | 失败语义 |
|---|---|---|
| `GET /evaluations` | `200` | `401` 未登录；`403` 只读角色 |
| `GET /evaluations/runs` | `200` | 同上 |
| `GET /evaluations/runs/{run_id}` | `200` | `404 NOT_FOUND` 运行不存在 |
| `GET /evaluations/{dataset_id}/cases` | `200` | `404 NOT_FOUND` 数据集不存在 |
| `POST /evaluations/{dataset_id}/run` | `200` | `400 UNKNOWN_TARGET` 变体非法；`404 NOT_FOUND` 数据集不存在或无启用样本；`409 DATASET_VERSION_CONFLICT`；`503 EVALUATION_DISABLED` |
| `GET /evaluations/{dataset_id}/comparison` | `200` | `404 NOT_FOUND` 数据集不存在 |

### `GET /evaluations`

```json
{
  "items": [
    {
      "id": 1,
      "name": "weekly-agent-quality",
      "version": "v1",
      "description": "周报质量基线数据集：覆盖正常增长、数据缺失、README 注入三类样本",
      "created_by": 1,
      "created_at": "2026-10-12T12:00:00+00:00"
    }
  ]
}
```

`limit` 默认 `50`，上限 `200`，按创建时间倒序。

### `GET /evaluations/runs`

查询参数 `dataset_id`、`target_type`、`limit`（默认 `50`）。返回每个变体的一次执行，最新在前：

```json
{
  "items": [
    {
      "id": 2,
      "dataset_id": 1,
      "target_type": "agent_full",
      "status": "succeeded",
      "agent_config_id": 1,
      "started_at": "2026-10-12T12:00:00+00:00",
      "finished_at": "2026-10-12T12:00:00+00:00",
      "summary": { "target_type": "agent_full", "reference_target": "baseline_single_prompt", "...": "..." }
    }
  ]
}
```

`status` ∈ `pending|running|succeeded|failed`。`baseline_single_prompt` 的 `agent_config_id` 为 `null`（它不是 Agent 配置），其余四个变体必须绑定一个。

### `GET /evaluations/runs/{run_id}`

在上面一条的基础上追加该运行的逐样本测量：

```json
{
  "id": 2,
  "dataset_id": 1,
  "target_type": "agent_full",
  "status": "succeeded",
  "agent_config_id": 1,
  "started_at": "...",
  "finished_at": "...",
  "summary": { "...": "..." },
  "results": [
    {
      "id": 4,
      "eval_case_id": 1,
      "case_key": "growth-surge",
      "status": "succeeded",
      "citation_coverage": "1.0000",
      "factual_consistency": "1.0000",
      "task_completion": "1.0000",
      "cost": "0.000000",
      "latency_ms": 0,
      "human_score": null,
      "details": { "source_accuracy": "1.0000", "tool_success_rate": "1.0000", "...": "..." }
    }
  ]
}
```

比率以字符串返回（四位小数），未行使的指标为 `null`（不是 `0`）。

### `GET /evaluations/{dataset_id}/cases`

查询参数 `enabled_only`（默认 `false`）。按 `case_key` 升序：

```json
{
  "items": [
    {"id": 1, "case_key": "growth-surge", "topic": "AI Agent", "enabled": true, "tags": ["growth", "happy-path"]}
  ]
}
```

样本不返回 `reference_json` / `rubric_json`：参考答案不通过 API 外泄，评分在服务端完成。

### `POST /evaluations/{dataset_id}/run`

请求体可省略；`targets` 省略表示按报告顺序执行全部五个变体：

```json
{"targets": ["agent_full", "baseline_single_prompt"]}
```

响应是统一对比报告（与 `GET .../comparison` 同构）：

```json
{
  "dataset": {"id": 1, "name": "weekly-agent-quality", "version": "v1"},
  "case_count": 3,
  "targets": [
    {
      "target_type": "baseline_single_prompt",
      "run_id": 1,
      "status": "succeeded",
      "case_count": 3,
      "scored_case_count": 3,
      "failed_case_count": 0,
      "metrics": {
        "citation_coverage": "0.0000",
        "factual_consistency": "1.0000",
        "task_completion": "1.0000",
        "source_accuracy": null,
        "tool_success_rate": null
      },
      "total_cost": "0.000000",
      "total_tokens": 0,
      "mean_latency_ms": 0,
      "prompt_injection_block_rate": null,
      "tool_call_total": 0,
      "tool_call_denied": 0,
      "verification_sufficient_rate": null,
      "reference_target": null,
      "deltas_vs_reference": {},
      "gate_failures": [
        "citation_coverage 0.0000 < target 0.95",
        "factual_consistency 0.0000 < target 0.90"
      ],
      "regression": false
    },
    {
      "target_type": "agent_full",
      "run_id": 2,
      "status": "succeeded",
      "scored_case_count": 3,
      "metrics": {"citation_coverage": "1.0000", "factual_consistency": "1.0000", "task_completion": "1.0000", "source_accuracy": "0.9630", "tool_success_rate": "0.9744"},
      "tool_call_total": 36,
      "tool_call_denied": 0,
      "verification_sufficient_rate": "0.6667",
      "reference_target": "baseline_single_prompt",
      "deltas_vs_reference": {"citation_coverage": "1.0000", "factual_consistency": "1.0000", "task_completion": "0.0000"},
      "gate_failures": [],
      "regression": false
    }
  ],
  "passed": true,
  "regressions": ["agent_no_verifier", "agent_no_tools"],
  "failed_gates": {
    "baseline_single_prompt": ["citation_coverage 0.0000 < target 0.95", "factual_consistency 0.0000 < target 0.90"],
    "agent_no_tools": ["citation_coverage 0.0000 < target 0.95", "factual_consistency 0.0000 < target 0.90"],
    "agent_no_verifier": ["citation_coverage 0.0000 < target 0.95", "factual_consistency 0.0000 < target 0.90"]
  }
}
```

契约要点：

- `reference_target` 决定 `deltas_vs_reference` 与 `regression` 的参照对象：`agent_full` 参照 `baseline_single_prompt`，三个消融参照 `agent_full`，`baseline_single_prompt` 为 `null` 且差值为空对象。这是"消融回答 `docs/evaluation.md` §8、候选回答 §9"两条问题在数据上的体现。
- `passed` **只裁决 `agent_full`**（`PROMOTION_TARGETS`）；消融是对照，其损失通过自身 `deltas_vs_reference` 与 `failed_gates` 呈现，不会否决发布。`regressions` 则列出**所有**发生质量回退的变体（因此 `passed=true` 与 `regressions` 非空可以同时成立）。无 `agent_full` 的诊断性 sweep 按其包含的全部变体裁决；任何变体的 `scored_case_count` 为 `0` 时 `passed` 为 `false`。
- `failed_gates` 的文案形如 `<metric> <actual> < target <threshold>`，阈值来自 `ARCHITECTURE.md` §12；`gate_failures` 为空的变体才可能通过。
- 每个变体独立一行 `eval_runs`，逐样本测量受 `UNIQUE(eval_run_id, eval_case_id)` 约束 —— 重跑刷新测量而不追加行，但**每次 sweep 都为每个变体新建一运行行**，因此历史执行不被改写。

### `GET /evaluations/{dataset_id}/comparison`

返回该数据集上**每个变体最近一次运行**的对比报告，不触发任何执行。响应与 `POST .../run` 同构，可复现同一份指标与差值，用于只读复核。

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
