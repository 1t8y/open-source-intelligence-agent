# Agent 工具接口契约

> 适用模块：`src/open_source_intelligence/agent/tools/`  
> 契约版本：v1.2  
> 最后更新：2026-10-12

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

- `limit` 范围为 1-20，默认 10。
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

- `section_key` 只能来自报告模板白名单：`summary`、`trend`、`highlights`、`risks`、`recommendation`、`appendix`。
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

## 11. 实现状态（T7）

自 2026-10-12 起，本契约由运行时 Pydantic 模型实现，并通过真实 MySQL 冒烟验证。

| 契约要素 | 实现位置 |
|---|---|
| 返回信封与错误码 | `agent/tools/contracts.py`（`ToolResult`、`ToolError`、`ToolMeta`） |
| 输入输出模型 | `agent/tools/contracts.py`，每个工具一套 Input/Output 模型，统一 `extra="forbid"`、`strict=True` |
| 白名单、ACL、预算、只读强制、调用记录 | `agent/tools/registry.py:ToolRegistry.invoke()` |
| 运行时上下文 | `agent/tools/context.py:ToolContext` |
| 主题范围授权 | `agent/tools/access.py` |
| 文本清理与截断 | `core/text.py`（`clean_untrusted_text` / `truncate_text` / `content_digest`） |

数据来源与读写边界：

| 工具 | 数据表 | 只读 |
|---|---|---|
| `query_repo_metrics` | `repo_snapshots` | 是 |
| `get_repository_profile` | `repositories`、`data_sources` | 是 |
| `get_repository_content` | `repository_contents` | 是 |
| `search_prior_reports` | `reports`、`report_sources` | 是 |
| `save_report_section` | `report_drafts`、`report_sections` | 否，仅写当前运行草稿 |

每次调用写入一行 `tool_calls`，其中 `arguments_json` 与 `result_summary` 在落库前脱敏并截断。`ToolRegistry.invoke()` 对只读工具强制检查会话脏状态：只读工具一旦产生写入即返回 `TOOL_INTERNAL_ERROR`，并**先回滚会话再记录该次调用**。回滚是必需的，因为记录调用本身会 `commit`；若只报错不回滚，处理器暂存的写入会被一并提交，让「只读工具从不写入」从一条可观测规则退化成一句口号。同一保护覆盖「处理器暂存写入后抛异常」的路径。

`agent_configs.tool_policy_json` 提供运行级工具白名单，运行时据此构造 `ToolContext.allowed_tools`；注册表自身的 `WHITELIST` 是上级边界，两者取交集。`repository_contents`、`report_drafts`、`report_sections` 是 T7 为落地本契约新增的表，详见 [data-model.md](data-model.md)。

`repository_contents` 的写入方是每日流水线的 `contents` 阶段（T23），不是 Agent：`get_repository_content` 只读缓存，**缓存为空时它返回 `TOOL_NOT_FOUND` 而不是去抓**。这是刻意的 —— 研究运行不该在一次运行中间发起任意出站请求，读不到就如实报读不到。

失败观察与结果观察的形状由 `agent/llm/messages.py` 的 `tool_error_payload` / `is_tool_error_payload` 一对定义（T23）：失败是 `{"error": {"code", "message"}}`，成功是该工具自己的载荷。两者都是 JSON 对象，所以**任何从对话里回收证据的代码必须显式排除失败壳** —— `agent/llm/offline.py:_collect_evidence` 是唯一这样的地方，它把一次 `TOOL_NOT_FOUND` 记成"读到了内容"的话，草稿就会引用一份从未到达模型的 README，然后 `reports generate` 在解析该引用时硬失败。授权拒绝（`TOOL_ACCESS_DENIED`）与未命中一样是失败壳，模型可以据此改换目标仓库，但两者都不能当证据。

## 12. 调用方与参数校验通道（T8）

自 2026-10-12 起，上述工具由 T8 的 Agent 运行时 `agent/graph.py` 在 `tool` 节点逐个调用，不再由测试或冒烟脚本直接驱动；运行时把 `plan` 节点返回的每个工具调用交给 `ToolRegistry.invoke()`，因此白名单、ACL、预算、Schema 校验与 `tool_calls` 记录对模型行为同样生效。工具白名单同时通过 `agent/tooling.py:build_tool_schemas()` 以 JSON Schema 形式暴露给模型，模型看到的契约与运行时校验同源。

`ToolRegistry` 的参数校验是双通道的：

```text
1. model_validate(arguments)                        # T7 严格 Python 路径，语义不变
2. 失败时回落 model_validate_json(json.dumps(...))  # JSON 路径，接受 ISO 8601 时间字符串
```

模型给出的参数天然是 JSON 形状，时间字段是字符串而非 `datetime`，第一条路径会拒绝。回落通道让同一份契约同时服务 Python 调用方与模型调用方，两条路径都保持 `extra="forbid"` 并拒绝错误类型；区别只在错误消息由哪条路径产生。已通过 Python 校验的调用方行为因此完全不变。
