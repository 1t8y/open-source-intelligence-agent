# 企业开源技术情报 Agent 与自动周报平台

> 项目状态：T1-T7 已完成，进入 T8
> 文档版本：v0.6
> 最后更新：2026-10-12

## 最终版路径

本项目所有代码、文档、配置和测试的最终版本统一存放于：

```text
D:\job-workspace\project_sources\open-source-intelligence-agent
```

其他位置即使存在副本，也只作为临时工作副本，不作为最终交付或验收依据。

## 项目定位

面向研发战略、架构和产品团队，自动采集公开开源项目动态，使用专用研究 Agent 完成趋势分析、证据补齐、质量验证和周报生成，并通过风险策略决定自动发布或进入人工审核。

本项目是老项目 `learning-planning-multi-agent` 的互补项目：

| 项目 | 主要证明能力 |
|---|---|
| 多智能体学习规划助手 | LangGraph、多 Agent、RAG、交互式 AI 应用 |
| 本项目 | 专用 Agent 工具调用、自动化 Pipeline、MySQL、工程化、评测与监控 |

## 文档索引

| 文档 | 用途 |
|---|---|
| [REQUIREMENTS.md](REQUIREMENTS.md) | 根需求基线、范围与验收标准 |
| [INTERFACES.md](INTERFACES.md) | 根接口基线，汇总 API、Tool 和跨模块对象 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 唯一真理源，约束架构、接口、数据和实施计划 |
| [docs/requirements.md](docs/requirements.md) | 详细产品需求、角色、功能与验收标准 |
| [docs/api-contract.md](docs/api-contract.md) | REST API、请求响应对象和错误约定 |
| [docs/agent-tool-contract.md](docs/agent-tool-contract.md) | Agent 内部工具、权限、错误码和测试契约 |
| [docs/data-model.md](docs/data-model.md) | MySQL 表结构、关系和运行时不变量 |
| [docs/security-and-privacy.md](docs/security-and-privacy.md) | 权限、数据保护、模型调用和审计边界 |
| [docs/evaluation.md](docs/evaluation.md) | Agent、报告质量和成本评测方案 |
| [docs/development-process.md](docs/development-process.md) | 企业流程的个人开发精简执行版本 |
| [docs/technology-learning-checklist.md](docs/technology-learning-checklist.md) | 按实施任务排列的技术学习清单 |
| [docs/readiness-and-startup-roadmap.md](docs/readiness-and-startup-roadmap.md) | 准备度检查和正式开工路线图 |
| [docs/adr](docs/adr) | 关键架构决策记录 |

## 当前阶段

当前 T7 已完成：五个白名单工具（`query_repo_metrics`、`get_repository_profile`、`get_repository_content`、`search_prior_reports`、`save_report_section`）的 Pydantic 契约、注册表（白名单 / ACL / 预算 / 只读强制）、截断与不可信标记、草稿幂等保存全部落地，并新增 Agent 运行、报告与 README 内容缓存共 9 张表。下一步进入 T8 LangGraph Research Agent。

T1 验证命令：

```powershell
.\scripts\verify_t1.ps1
```

该脚本会强制重建镜像，并执行 pytest、Ruff、mypy、Compose、API、Worker 和 Beat 验证。

T2 验证命令：

```powershell
.\scripts\verify_t2.ps1
```

该脚本使用一次性 MySQL 数据库验证迁移升级、回滚、再次升级和模型一致性，不修改正式开发库。

T3 验证命令：

```powershell
.\scripts\verify_t3.ps1
```

该脚本会重建服务、应用迁移、引导临时管理员、验证登录和当前用户、检查 401，并确认审计记录已持久化。

T4 验证命令：

```powershell
.\scripts\verify_t4.ps1
```

该脚本执行真实双源采集，并重复采集一次确认仓库数量不变。

T5 验证命令：

```powershell
.\scripts\verify_t5.ps1
```

该脚本在现有真实仓库上执行 3 次快照、2 次候选排序，验证幂等、窗口增量、排序和审计后清理验证数据。

T6 验证命令：

```powershell
.\scripts\verify_t6.ps1
```

该脚本执行每日 Pipeline 的 MySQL 冒烟（运行记录、attempt 轨迹、错误脱敏、幂等和 CHECK 约束），通过真实 Celery broker 和 Worker 调度任务，用一个专用 Worker 指向不可达数据源以证明失败重试留下两条尝试记录，验证 Beat 定时触发与 CLI 状态查询，最后清理验证数据。

T7 验证命令：

```powershell
.\scripts\verify_t7.ps1
```

该脚本执行 pytest、Ruff、mypy，重建服务并检查迁移漂移，然后在真实 MySQL 上调用全部 5 个白名单工具，验证 Schema 严格校验、跨主题 ACL 拒绝、内容清理与截断、不可信标记、草稿幂等与版本递增、只读工具零写入以及 `tool_calls` 记录，最后清理验证数据。

## 开发原则

1. 数据采集和指标计算保持确定性，Agent 负责研究决策、证据补齐、验证和综合写作。
2. 常规低风险周报采用 `AUTO_WITH_DELAY`，不为追求安全而默认阻塞人工审核。
3. 企业权限、审计、模型数据保护和 Token 成本以轻量方式实现，保留扩展接口，不宣称完成完整 DLP、ABAC 或多租户。
4. 每次代码变更若影响架构、接口、数据模型、依赖或目录结构，必须同步回写 `ARCHITECTURE.md`。
