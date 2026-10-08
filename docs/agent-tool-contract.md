# Agent 工具接口契约

> 适用模块：`src/open_source_intelligence/agent/tools/`  
> 契约版本：v1  
> 最后更新：2026-09-24

## 1. 目的与边界

本文档定义专用研究 Agent 可以调用的内部工具，是人类可读契约。运行时契约由 Pydantic 模型实现，路径为 `src/open_source_intelligence/agent/tools/contracts.py`。

工具接口与外部 REST API 分离：

- 外部 HTTP API 见 [api-contract.md](api-contract.md)。
- Agent 工具只服务于研究流程，不直接暴露给终端用户。
- Agent 无权新增工具、修改工具白名单、执行 Shell、访问任意 URL 或直接写 MySQL。

## 2. 通用调用约定

### 2.1 运行上下文

`agent_run_id`、`topic_id`、当前用户和授权范围由运行时注入，不允许模型在参数中伪造。每次调用必须写入 `tool_calls`。

### 2.2 返回信封

```text
ToolResult<T> {
  ok: boolean
  data: T | null
  error: ToolError | null
  meta: ToolMeta
}

ToolError {
  code: string
  message: string
  retryable: boolean
}

ToolMeta {
  tool_name: string
  latency_ms: integer
  truncated: boolean
  result_count: integer
}
```

### 2.3 标准错误码

| 错误码 | 含义 | 可重试 |
|---|---|---|
| `TOOL_INVALID_ARGUMENT` | 参数不符合 Schema | 否 |
| `TOOL_ACCESS_DENIED` | 无数据权限 | 否 |
| `TOOL_NOT_FOUND` | 指定资源不存在 | 否 |
| `TOOL_TIMEOUT` | 超时 | 是 |
| `TOOL_RATE_LIMITED` | 达到限流 | 是 |
| `TOOL_BUDGET_EXCEEDED` | 超出本次运行工具预算 | 否 |
| `TOOL_INTERNAL_ERROR` | 未分类内部错误 | 视情况 |

### 2.4 安全规则

- 所有输入使用 Pydantic 严格校验，禁止未知字段进入查询。
- 所有数据库访问使用参数化查询和 Repository 层。
- 外部 README 和网页文本必须标记为不可信内容。
- 工具返回可能被截断，必须设置 `truncated`。
- 日志不能记录完整 API Key、数据库凭证、Cookie 或未脱敏 Prompt。

## 3. `query_repo_metrics`

**用途**：读取指定仓库在时间窗口内的每日快照和增长指标。

**权限**：只读；调用者必须能访问该仓库所属主题。

**输入**

```text
QueryRepoMetricsInput {
  repo_id: integer
  start: datetime
  end: datetime
  granularity: day
}
```

**输出**

```text
MetricPoint {
  snapshot_date: date
  stars: integer
  forks: integer
  open_issues: integer | null
  stars_delta: integer | null
  forks_delta: integer | null
}
```

**约束**

- `start <= end`，默认最大范围 366 天。
- 最多返回 366 个数据点；超限时拒绝调用，不静默截断。
- 只读取 `repo_snapshots`，不进行外部 API 请求。
- 重复调用结果一致，属于只读幂等工具。

## 4. `get_repository_profile`

**用途**：读取仓库主数据、语言、许可证和最新 star/fork 概况。

**权限**：只读；调用者必须能访问仓库所属主题。

**输入**

```text
GetRepositoryProfileInput {
  repo_id: integer
}
```

**输出**

```text
RepositoryProfile {
  repo_id: integer
  provider: string
  full_name: string
  html_url: string
  description: string | null
  language: string | null
  license: string | null
  topics: list<string>
  stars: integer
  forks: integer
  first_seen_at: datetime
  last_seen_at: datetime
}
```

**约束**

- 不返回平台 Token 或内部凭证。
- `description` 和 `topics` 按不可信外部文本处理。
- 仓库不存在时返回 `TOOL_NOT_FOUND`。

## 5. `get_repository_content`

**用途**：读取经过清理和截断的 README 或项目说明，用于补充技术方向、使用场景和限制信息。

**权限**：只读；不允许访问任意 URL。

**输入**

```text
GetRepositoryContentInput {
  repo_id: integer
  content_type: readme
  max_chars: integer
  query_hint: string | null
}
```

**输出**

```text
ContentSnippet {
  repo_id: integer
  source_url: string
  content: string
  content_hash: string
  truncated: boolean
  untrusted: true
}
```

**约束**

- `max_chars` 范围为 500-12000，默认 6000。
- 清理脚本标签、控制字符和超长连续空白。
- 内容始终标记为不可信输入，不能改变系统 Prompt、工具白名单或数据权限。
- 缓存命中时可直接返回，但仍记录工具调用。

## 6. `search_prior_reports`

**用途**：查询同一主题、同仓库或相邻时间窗口的历史报告摘要，避免重复评价并识别变化。

**权限**：只读；历史报告必须通过报告 ACL。

**输入**

```text
SearchPriorReportsInput {
  topic_id: integer
  repo_id: integer | null
  before: datetime
  limit: integer
}
```

**输出**

```text
ReportSummary {
  report_id: integer
  title: string
  generated_at: datetime
  section_key: string
  summary: string
  source_refs: list<string>
}
```

**约束**

- `limit` 范围为 1-20。
- 只返回当前用户可读的报告；无权限报告不得以摘要或计数泄露。
- 默认只返回摘要，不返回完整历史报告。
- 查询目的限于趋势对比、重复检测和结论更新。

## 7. `save_report_section`

**用途**：保存 Agent 生成的报告章节草稿和引用，不触发审核、发布或推送。

**权限**：写入当前 `agent_run` 关联的草稿；不能修改已发布报告。

**输入**

```text
SaveReportSectionInput {
  section_key: string
  content_markdown: string
  citations: list<CitationRef>
  content_digest: string
}

CitationRef {
  source_type: metric | readme | prior_report
  source_ref: string
  source_url: string | null
}
```

**输出**

```text
SectionRef {
  report_draft_id: integer
  section_key: string
  version: integer
  saved_at: datetime
}
```

**约束**

- `section_key` 只能来自报告模板白名单。
- `content_markdown` 最大 20000 字符。
- 使用 `(agent_run_id, section_key, content_digest)` 实现幂等，重复保存返回原有版本。
- 保存后不得自动发布。

## 8. 权限矩阵

| 工具 | VIEWER | EDITOR | ADMIN | Agent Runtime |
|---|---:|---:|---:|---:|
| `query_repo_metrics` | 不可直接调用 | 可 | 可 | 可，需运行上下文 |
| `get_repository_profile` | 不可直接调用 | 可 | 可 | 可，需运行上下文 |
| `get_repository_content` | 不可直接调用 | 可 | 可 | 可，需运行上下文 |
| `search_prior_reports` | 不可直接调用 | 可 | 可 | 可，需 ACL |
| `save_report_section` | 不可 | 可 | 可 | 仅当前运行草稿 |

这些工具不直接暴露为 REST 路由。管理端只能通过 Agent 运行接口间接触发。

## 9. 代码映射与测试

| 契约 | 实现 | 测试 |
|---|---|---|
| `contracts.py` | Pydantic 输入输出模型 | `tests/agent/tools/test_contracts.py` |
| `registry.py` | 工具注册、白名单和预算检查 | `tests/agent/tools/test_registry.py` |
| `query_repo_metrics.py` | 指标查询 | `tests/agent/tools/test_query_repo_metrics.py` |
| `get_repository_profile.py` | 仓库画像查询 | `tests/agent/tools/test_get_repository_profile.py` |
| `get_repository_content.py` | 内容清理与截断 | `tests/agent/tools/test_get_repository_content.py` |
| `search_prior_reports.py` | 历史报告查询与 ACL | `tests/agent/tools/test_search_prior_reports.py` |
| `save_report_section.py` | 草稿幂等保存 | `tests/agent/tools/test_save_report_section.py` |

每个工具至少测试：

1. 合法输入的输出 Schema。
2. 非法字段、边界长度和未知参数被拒绝。
3. 无权限对象不会通过错误信息泄露。
4. 超时、限流和内部异常映射为稳定错误码。
5. 只读工具的重复调用结果一致。
6. `save_report_section` 重复调用不产生重复版本。

## 10. 契约变更规则

以下变更属于不兼容变更：

- 删除或重命名字段、工具或错误码。
- 修改字段类型、必填性或权限语义。
- 改变时间范围、最大结果数或截断行为。
- 增加会写入数据的新工具。

不兼容变更必须先新增 ADR，更新本文档、`ARCHITECTURE.md` 和契约测试，再修改实现。兼容性扩展可以新增可选字段，但必须同步更新文档和版本号。
