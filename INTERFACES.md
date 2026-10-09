# 项目接口基线

> 文档状态：待用户确认  
> 文档版本：v0.1  
> 最后更新：2026-10-08  
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
| 数据源 | `/sources` | ADMIN/EDITOR | 不保存明文凭证 |
| 主题 | `/topics`、`POST /topics/{id}/run` | ADMIN/EDITOR | 过滤条件、候选上限、发布模式 |
| 仓库 | `/repositories`、`/repositories/{id}/snapshots` | 有主题权限 | 趋势和快照查询 |
| Agent | `/agent/runs`、`/agent/runs/{id}/steps` | ADMIN/EDITOR | 运行状态、预算和轨迹 |
| 报告 | `/reports`、`review`、`publish`、`push` | ADMIN/EDITOR | 版本、状态机、ACL 和审核 |
| 评测 | `/evaluations` | ADMIN/EDITOR | 数据集、执行、指标和基线差异 |
| 监控 | `/metrics/overview` | ADMIN/EDITOR | 任务、Token、延迟、失败率 |
| 审计 | `/audit-logs` | ADMIN | 只读，不可修改 |

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
    -> collectors/pipeline/agent/reports
      -> persistence/core
```

跨层接口必须通过明确的请求对象和结果对象，不允许 Web 层直接调用 SQL，也不允许 Agent 直接访问数据库 Session。

| 模块 | 对外能力 | 主要消费方 |
|---|---|---|
| `collectors` | `collect_source`、返回标准仓库数据 | pipeline/workers |
| `pipeline` | 清洗、快照、候选排序 | workers/api |
| `agent.runtime` | `run_research_agent` | workers |
| `agent.tools` | 白名单工具 | agent.runtime |
| `reports` | 草稿、验证、策略决策和发布 | api/workers |
| `security` | 登录、授权、脱敏和审计 | 所有模块 |
| `evaluation` | 数据集、Scorer 和回归 | api/workers |
| `workers` | Celery 应用、周期任务和 Worker 入口 | 部署/调度 |
| `persistence` | Repository 和事务边界 | 所有业务模块 |
| `migrations` | Alembic 升级、回滚和 schema drift 检查 | 开发/部署 |

Worker 接口：

```text
create_celery_app(settings: Settings | None = None) -> Celery
healthcheck() -> {"status": "ok", "task": "healthcheck"}
```

Celery CLI 入口为 `open_source_intelligence.workers.celery_entry:app`。首期 Beat 每 60 秒执行一次 `open_source_intelligence.workers.tasks.healthcheck`。

Persistence 接口：

```text
create_database_engine(database_url: str, *, echo: bool = False) -> Engine
create_session_factory(engine: Engine) -> sessionmaker[Session]
Base.metadata
```

首期模型覆盖 `users`、`roles`、`user_roles`、`audit_logs`、`data_sources`、`topics`、`repositories`、`repo_snapshots`、`topic_repositories` 和 `pipeline_runs`。

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
```

认证接口：`POST /api/v1/auth/login`、`GET /api/v1/auth/me`。RBAC 每次请求从数据库读取当前角色，不依赖 Token 中的历史角色快照。

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
  task_key: string
  repository_id: integer
  objective: string
  required_evidence: list<string>
  status: pending | running | completed | failed
}

EvidenceRef {
  source_type: metric | readme | prior_report
  source_ref: string
  repository_id: integer | null
  source_url: string | null
  excerpt: string | null
}

PublishDecision {
  mode: AUTO | AUTO_WITH_DELAY | MANUAL
  requires_review: boolean
  reason_codes: list<string>
  publish_at: datetime | null
}
```

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
- `agent_steps` 在运行内按 `step_index` 唯一递增。
- `MANUAL` 报告缺少 `approve` 审核记录时不能发布。

## 8. 错误码与状态机

发布状态机：

```text
draft
  -> pending_review
  -> approved
  -> scheduled
  -> published
```

异常分支：`failed`；退回分支：`changes_requested`。

常见业务错误：

| 错误码 | 场景 |
|---|---|
| `AUTH_REQUIRED` | 未登录或 Token 失效 |
| `FORBIDDEN` | 角色或 ACL 不通过 |
| `VALIDATION_ERROR` | 请求或工具参数非法 |
| `CONFLICT` | 唯一约束或状态机冲突 |
| `AGENT_BUDGET_EXCEEDED` | 超出循环、工具或 Token 预算 |
| `PUBLISH_POLICY_BLOCKED` | 报告不满足自动发布条件 |

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
