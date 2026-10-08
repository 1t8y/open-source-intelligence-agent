# 技术学习清单

> 目标：为 T1-T18 实施提供最短学习路径。  
> 原则：按模块学习，不要求先掌握全部技术。  
> 最后更新：2026-10-08

## 1. 使用方法

每个技术项包含“学习目标、项目用途、最小验证”。只有能完成最小验证，才进入对应任务。学习过程中优先写最小 demo，不系统抄教程。

## 2. 开工前必须掌握

| 优先级 | 技术 | 学习目标 | 最小验证 |
|---|---|---|---|
| P0 | Python 3.11+ | 虚拟环境、类型标注、异步与异常处理 | 写一个带类型标注的小型包，pytest 通过 |
| P0 | Git | 分支、提交、diff、回滚未提交改动 | 创建分支并完成一次小型提交 |
| P0 | Docker / Compose | 镜像、容器、网络、卷、环境变量 | 用 Compose 启动一个 API 和数据库 |
| P0 | FastAPI | 路由、依赖注入、请求模型、错误处理 | 实现 `/health` 和一组 CRUD 接口 |
| P0 | Pydantic 2 | BaseModel、字段校验、嵌套模型 | 为工具输入输出编写严格 Schema |
| P0 | pytest | fixture、参数化、集成测试 | 测试 API 成功和校验失败路径 |
| P1 | 配置与密钥 | `.env`、环境注入、Secret 引用 | 本地不提交真实 API Key |

## 3. 数据与工程学习模块

| 对应任务 | 技术 | 学习重点 | 最小验证 |
|---|---|---|---|
| T1 | 项目工程化 | `pyproject.toml`、包结构、日志、lint | 包可安装，空应用可启动 |
| T2 | SQLAlchemy 2 | ORM 模型、Session、事务、关系 | 完成两张表的事务读写 |
| T2 | Alembic | 初始化、迁移、升级、降级 | 创建并回滚一次迁移 |
| T2 | MySQL 8 | 数据类型、索引、唯一约束、EXPLAIN | 查询时间窗口并使用组合索引 |
| T3 | 认证与 RBAC | 密码散列、JWT/Cookie、权限依赖 | 三个角色出现 200/403 差异 |
| T3 | 审计日志 | 不可变事件、request_id、脱敏 | 记录登录、拒绝和配置变更 |
| T4 | HTTP 客户端 | 超时、重试、分页、HTTP 状态码 | 采集一页 GitHub 数据 |
| T4 | GitHub API | Search API、认证、速率限制、ETag | 处理限流和分页并保存最小字段 |
| T4 | 幂等设计 | 唯一键、Upsert、事务 | 同一批数据重复执行两次数量不变 |
| T5 | 时间序列 | 每日快照、窗口差分、UTC 与时区 | 计算 7 日 star/fork 增量 |
| T6 | Redis | 缓存、TTL、队列基础 | 缓存一次 API 响应并设置过期 |
| T6 | Celery | task、retry、状态、Beat | 定时任务失败后按策略重试 |
| T6 | 任务可观测 | pipeline_runs、错误分类、耗时 | 查询成功、部分失败和失败状态 |

## 4. Agent 学习模块

| 对应任务 | 技术 | 学习重点 | 最小验证 |
|---|---|---|---|
| T7 | Tool Calling | 工具 Schema、参数校验、工具结果 | 模型能选择合法工具并处理结果 |
| T7 | Pydantic 契约 | 输入输出模型、未知字段、错误码 | 非法参数不会进入业务逻辑 |
| T7 | Tool 安全 | 白名单、只读优先、ACL、截断 | 越权仓库返回 `TOOL_ACCESS_DENIED` |
| T8 | LangGraph | State、Node、Edge、条件边、结束条件 | 跑通 Research→Tool→Verify→Write |
| T8 | Agent 状态设计 | 共享状态、字段所有权、可恢复性 | 中断后可从持久化状态查询 |
| T8 | Prompt Engineering | 角色、约束、结构化输出、证据引用 | Prompt 改动可版本化 |
| T9 | Agent Guardrails | 最大循环、工具次数、Token 预算 | 超预算时安全停止 |
| T9 | Prompt Injection | 不可信内容隔离、工具越权检测 | 恶意 README 无法触发未知工具 |
| T10 | 结构化写作 | JSON/Markdown、章节模板、Citation | 每个关键结论可关联来源 |
| T11 | 验证器设计 | 数字一致性、引用验证、敏感内容 | 错误数字被 Verifier 标记 |
| T11 | 发布策略 | 状态机、风险等级、延迟发布 | 常规自动、异常进入审核 |

## 5. 评测与监控学习模块

| 对应任务 | 技术 | 学习重点 | 最小验证 |
|---|---|---|---|
| T14 | 评测集设计 | 样本版本、参考结论、Rubric | 建立至少一个可回归的数据集版本 |
| T14 | RAGAS 思维 | Faithfulness、Answer Relevancy 等指标思想 | 定义报告事实一致性指标 |
| T14 | LLM-as-a-judge | 结构化评分、参考证据、偏差控制 | 评分结果可复核且留理由 |
| T14 | 消融实验 | Baseline、去除 Tool、去除 Verifier | 输出指标变化和结论 |
| T15 | 指标设计 | 成功率、P95、Token、费用、人工干预率 | 指标可从数据库聚合 |
| T15 | 日志与 Trace | 结构化日志、request_id、Agent Step | 能从失败报告追到具体工具调用 |

## 6. 安全与部署学习模块

| 对应任务 | 技术 | 学习重点 | 最小验证 |
|---|---|---|---|
| T3/T17 | RBAC 与 ACL | 角色、资源范围、拒绝优先 | 列表和详情均不泄露无权数据 |
| T3/T17 | 数据保护 | 密钥注入、日志脱敏、模型策略 | API Key 不进入日志或 Prompt |
| T12 | Jinja2/HTMX | 服务端渲染、表单、CSRF | 完成报告查看和审核页面 |
| T13 | Webhook/邮件 | 幂等发送、失败重试、告警 | 同一报告不重复推送 |
| T16 | 数据回填 | 分页回填、断点续传、限流 | 完成一次历史回填演示 |
| T17 | 安全测试 | 越权、注入、错误信息泄露 | 核心安全用例全部通过 |
| T18 | Docker Compose | 多服务启动、健康检查、卷 | 新环境一键启动完整系统 |

## 7. 推荐学习顺序

```text
Python/Git/Docker
  -> FastAPI/Pydantic/pytest
  -> MySQL/SQLAlchemy/Alembic
  -> 认证/RBAC/审计
  -> HTTP/GitHub API/幂等
  -> Redis/Celery
  -> Tool Calling/Pydantic 契约
  -> LangGraph/Guardrails
  -> 报告验证/发布策略
  -> 评测/监控
  -> 安全测试/Docker Compose
```

不要等所有技术学完再开始 T1。推荐边学边做：

1. 学 Docker、FastAPI、配置，完成 T1。
2. 学 SQLAlchemy、Alembic、MySQL，完成 T2。
3. 学认证和审计，完成 T3。
4. 学 GitHub API、幂等和 Celery，完成 T4-T6。
5. 学 Tool Calling 和 LangGraph，完成 T7-T11。
6. 学评测、安全和部署，完成 T12-T18。

## 8. 暂不需要学习

首期不要求掌握 Kubernetes、多租户、复杂 ABAC、模型微调、Multi-Agent 自由协作和高可用集群。它们不阻塞项目启动，强行引入反而偏离当前范围。

## 9. 学习完成判定

每项技术达到以下状态即可开始对应任务：

1. 能解释它在项目中的必要性。
2. 能写出最小可运行示例。
3. 能说明一个常见失败场景。
4. 能通过对应任务的验证或冒烟测试。
