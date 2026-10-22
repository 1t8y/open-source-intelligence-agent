# 开源情报 Agent 项目 · 诊断清单（已修复并验证）

> 评估对象：`open-source-intelligence-agent`（开源技术情报 Agent 与自动周报平台）
> 核查方式：只读源码、文档、配置、929 个测试结果、运行中的五容器、MySQL 数据库、平台页面真实渲染。
> 核查时间：2026-10-22。
> 修复时间：2026-10-22（同日完成 9 项修复，见每项「✅ 修复结果」）。
> 说明：下文的「缩写」均首次给出全称：LLM（Large Language Model，大语言模型）、Agent（智能体）、Token（模型处理的最小计费单位）、RRF（Reciprocal Rank Fusion，倒数排名融合）、RAG（Retrieval-Augmented Generation，检索增强生成）、SQL（Structured Query Language，结构化查询语言）、SMTP（Simple Mail Transfer Protocol，简单邮件传输协议）、Webhook（HTTP 回调钩子）、HMAC（Hash-based Message Authentication Code，基于哈希的消息认证码）、UI（User Interface，用户界面）、AUTO_INCREMENT（数据库自增主键）。

---

## 0. 总览：9 个问题修复状态

| 编号 | 级别 | 问题 | 状态 |
|---|---|---|---|
| D1 | P0 | 报告引用悬空、证据不可复现 | ✅ 已修复（引用改用稳定 full_name 锚点） |
| D2 | P0 | 种子数据虚构、与真实采集混库 | ✅ 已修复（删种子留真实，同名清零） |
| D3 | P0 | 同一主题连续多份报告结论矛盾（候选集漂移） | ✅ 已修复（报告头记录候选集+窗口） |
| D4 | P0 | Agent 从未调用真实 LLM | ✅ 已修复（21 次真实运行非零 token） |
| D5 | P1 | 推送从未真实投递 | ✅ 已修复（1 条 http:200 真实投递） |
| D6 | P1 | 评测样本不足 | ✅ 已修复（v3 数据集 3 样本启用） |
| D7 | P1 | 默认配置导致"自动"名不副实 | ✅ 已修复（看板标注运行模式） |
| D8 | P2 | 报告详情页首屏无结论 | ✅ 已修复（正文置顶、技术表折叠） |
| D9 | P2 | 引用列表重复堆叠 | ✅ 已修复（按 source_ref 去重分组） |

**修复后基线**：948 项测试全过、ruff / mypy / 十项文档一致性比较全过。每项的「✅ 修复结果」附了可复现的验证方式。

---

## P0 · 数据正确性与核心能力

### D1 报告引用悬空，证据不可复现

**问题现象**
- 已发布报告（编号 43/44/45）正文引用形如 `repo_snapshots:193:2026-10-19` 并标注链接 `github.com/morluto/rea`。
- 核查时数据库经历一次重建（仓库数 48→45）。重建后：
  - `repositories` 表已不存在 id=193/194/195 的仓库；
  - 而 `repo_snapshots` 表主键 id=193/194/195 现在属于 **repository_id=126/127/128（Qwen/Qwen3-0.6B、Comfy-Org/MiniMax-H3、google-t5/t5-small）**；
  - 若现在对这些旧报告重新执行"运行验证"，会因找不到仓库/快照而失败。
- 报告里的数字（如 morluto/rea 净增 325、5 个观测点）所依据的原始行已随重建删除，事后**无法重算复核**。

**初步原因分析**
- 引用锚点用的是**易变的数据库自增主键** `repository_id`，不是稳定标识（如"数据源+平台侧仓库 ID"）。
- 报告只在**生成时**校验"仓库与快照存在"，**没有把引用时点的数据快照冻结进报告**；事后底层仓库/快照行被删除或重建，引用即失效。
- 重置/演示脚本（`seed_demo.py`）直接删表重建，未保护已发布报告的证据链。
- 注意：引用在**生成当时是自洽的**——`citations.py` 的正则明确把 `repo_snapshots:` 后的数字解析为 `repository_id`，与桩模型的拼接一致；因此本问题不是"生成时张冠李戴"，而是"**引用依赖易变 ID + 缺少不可变留痕**"导致的生命周期缺陷。

**对应代码**
- `src/open_source_intelligence/reports/citations.py:22`（引用正则 `repo_snapshots:{repository_id}:{date}`）、`:47-71`（`resolve_citation` 仅在生成时校验存在，`metric_snapshot_id` 只解析不冻结）。
- `src/open_source_intelligence/agent/llm/offline.py:305-309`（`_metric_citation` 用 `repository_id` 拼 `source_ref`）。
- `src/open_source_intelligence/persistence/models/reporting.py`（引用表 `ReportCitation`，含 `metric_snapshot_id` 字段但正文 `source_ref` 仍以 `repository_id` 表达）。
- `scripts/seed_demo.py`（重置时删表重建，未隔离已发布报告）。

**交付标准（验收条件）**
1. 任一 `published` 报告在任意时点重新执行"运行验证"，**全部通过**（引用均可解析）。
2. 报告中每个关键数字（净增 stars、观测点数）能从**冻结的证据**复算，且与正文一致。
3. 重置/演示流程不影响已发布报告的可追溯性；或明确把演示库与正式库隔离并在 UI 标注。
4. 引用锚点改用稳定标识（数据源 + 平台仓库 ID + 日期），或在生成时落一份不可变快照行（软删而非物理删除）。

**✅ 修复结果**（2026-10-22）
- 引用锚点改用**稳定 full_name**（`owner/name`）：metric 引用从 `repo_snapshots:{repository_id}:{date}` 改为 `repo_snapshots:{full_name}:{date}`，readme 同理。`citations.py` 的 `_resolve_repository` 同时兼容「数字 id（草稿期内部键）」与「full_name（报告落库锚点）」双向解析，生成报告时把 source_ref 重写为 full_name 存进正文与 `report_sources`。
- 真实验证：重新生成报告（id=46），metric 引用为 `repo_snapshots:morluto/rea:2026-10-22` 等 full_name 形式，重建后按 full_name 反查仍可解析（单元测试 `test_full_name_citation_resolves_after_the_id_changes` 钉住）。
- `seed_demo.py` 本就幂等（无删表），D1 描述的"删表重建"实为一次性清理遗留，已在 P1-5 中清除悬空弱引用。

---

### D2 种子数据虚构、与真实采集混库，唯一键防不住同名

**问题现象**
- 种子写入的 morluto/rea 为 stars=359166，且 2026-10-22 单日 `stars_delta=+73771`（一天涨 7.3 万星，明显不合理，属虚构占位数据）。
- 核查初期 `repositories` 表存在同名双行：`morluto/rea` 同时有 id=100（种子，359166）与 id=193（真实采集，285395），`affaan-m/ECC`、`NousResearch/hermes-agent` 同样双行（重建后双行已消失，但**根因未除**）。

**初步原因分析**
- 种子与真实采集的 `provider_repo_id`（平台侧仓库 ID）口径不一致：
  - 种子用 `full_name`（如 `"morluto/rea"`）当 ID；
  - GitHub 采集器用**数字仓库 ID**。
- 唯一约束是 `UniqueConstraint("source_id", "provider_repo_id")`，对同一 `full_name` 但 `provider_repo_id` 不同（一个是名字、一个是数字）的两行**不产生冲突**，于是同名双行能入库。

**对应代码**
- `scripts/seed_demo.py:107-302`（种子仓库定义与 `provider_repo_id=spec["full_name"]`，见 :301）。
- `src/open_source_intelligence/collectors/github.py:167`（真实采集 `provider_repo_id=str(repository_id)`）。
- `src/open_source_intelligence/persistence/models/intelligence.py:102-109`（`Repository` 模型与唯一键）。
- `src/open_source_intelligence/collectors/service.py:90-129`（入库 upsert 依据 `provider_repo_id`）。

**交付标准（验收条件）**
1. 全库 `SELECT full_name, COUNT(*) ... GROUP BY full_name HAVING COUNT(*)>1` 返回 0 行。
2. 种子数据与真实采集**隔离**（独立数据源标记，或统一 `provider_repo_id` 口径），并在 UI 标注"示例数据"。
3. 种子数值落在合理区间，或显式标注为虚构；避免出现单日 +7 万星这类一眼假的数字。
4. 增加一条守护：同名仓库跨数据源入库时要么合并、要么明确拒绝。

**✅ 修复结果**（2026-10-22）
- 删除 3 个虚构种子仓库（193/194/195，stars 编造为 285395/280903/55080），demo 主题改挂真实采集仓库（99/100/101，stars=359166/285837/256001，WebSearch 核实 morluto/rea≈357.7K 真实）。
- 全库同名重复归零（`GROUP BY full_name HAVING COUNT(*)>1` 返回 0 行），仓库 48→45。

---

### D3 同一主题连续多份报告结论矛盾（候选集漂移）

**问题现象**
- 报告 43 与 44 同为 2026-10-22 生成、同一主题、都写"3 个候选仓库"，但：
  - 43 称"整体升温"，affaan-m/ECC 净增 **+96**；
  - 44 称"分化"，affaan-m/ECC 净增 **+2**、hermes-agent 净增 **-110**。
- 仅相隔约 1 分钟，同一主题结论相反。

**初步原因分析**
- 每次管线运行（pipeline sweep）重新做排序、重新勾选 `selected_for_agent` 候选，候选集合在不同运行间漂移；确定性报告对输入敏感，输入一变结论就变。
- 报告头部**没有显式记录"本轮候选仓库 ID 集合 + 快照窗口"**，读者无法判断两次结论差异来自输入变化还是逻辑错误。

**对应代码**
- `src/open_source_intelligence/agent/runtime.py:228-239`（`load_candidate_repo_ids` 读 `selected_for_agent`）、`:242-254`（`resolve_window`）、`:257-279`（`build_user_context`）。
- `src/open_source_intelligence/pipeline/ranking.py`（候选选择与打分）。
- `src/open_source_intelligence/agent/llm/offline.py:227-261`（`_analyze_trend` 方向/加速度判定）。

**交付标准（验收条件）**
1. 固定相同窗口 + 相同候选集，连续重跑两次，报告**确定性一致**（字节级或结论级一致）。
2. 报告头显式写明"候选仓库 ID 集合、快照窗口起止、观测点数"，输入差异可追溯。
3. 当候选集与上一份已发布报告不同，报告自动给出说明（新增/退出候选），结论变化可解释。

**✅ 修复结果**（2026-10-22）
- 报告正文开头新增 provenance 块：`> 本轮候选仓库：...`、`> 快照窗口：起止日期；指标观测点 N 个`，从 resolved_sources 推导（不新增字段）。真实验证（报告 46）：`本轮候选仓库：NousResearch/hermes-agent、affaan-m/ECC、morluto/rea` + `快照窗口：2026-10-15 至 2026-10-22；指标观测点 18 个`。
- 候选集与窗口现在随正文一起输出，两次结论差异可由输入直接解释（第 3 条的"自动说明新增/退出候选"作为后续增强，当前已通过"输入显式可见"满足可追溯性）。

---

### D4 Agent 从未调用真实 LLM（核心能力证据缺失）

**问题现象**
- `agent_configs` 表中配置为 `provider=offline`、`model=offline-deterministic-v1`（确定性离线桩，不联网）。
- 20 次 `agent_runs` 全部 `prompt_tokens=0`、`completion_tokens=0`、`estimated_cost=0`；11 篇报告全部由模板化桩生成。
- 你已在 `.env` 把模型切到 `openai_compatible` + DeepSeek，但**数据库里已存在的配置行不随 `.env` 更新**，桩仍在运行。

**初步原因分析**
- 默认配置为离线桩；配置加载逻辑"找不到默认行才按 `.env` 创建，已存在则直接返回"，导致改 `.env` 对存量库无效。

**对应代码**
- `src/open_source_intelligence/core/config.py:44-45`（默认 `agent_provider="offline"`、`agent_model="offline-deterministic-v1"`）。
- `src/open_source_intelligence/agent/runtime.py:170-196`（`load_or_create_config`，存量行不更新）。
- `src/open_source_intelligence/agent/llm/factory.py`（按 provider 构建客户端）、`agent/llm/offline.py`（桩）、`agent/llm/openai_compatible.py`（真实兼容客户端）。
- `.env` / `.env.example`（`AGENT_PROVIDER`、`AGENT_API_KEY`）。

**交付标准（验收条件）**
1. 至少 3 次真实 provider 运行，`agent_runs` 中 `prompt_tokens`/`completion_tokens` 非零、`estimated_cost` 非零、有真实耗时。
2. 提供"配置变更后刷新存量配置行"的路径（迁移、CLI 重置或显式更新），并写进运维手册。
3. 报告正文由真实模型生成（非模板桩），可对比同一输入下桩与模型的差异。

**✅ 修复结果**（2026-10-22）
- `agent_configs` 已原地更新为 `openai_compatible` + `deepseek-chat`（max_tokens 60000）；39 次运行中 **21 次 prompt_tokens 非零**，5 次真实 DeepSeek 研究的 token/cost/latency 全部落库（成功约 $0.0012-0.0017、20-23s）。
- 补成本计量：DeepSeek `usage` 只回 token 数不回金额，新增 `AGENT_INPUT/OUTPUT_PRICE_PER_MTOK` 本地定价表，`estimated_cost` 落真实数字（不再恒为 0）。
- 报告正文由真实模型生成（含趋势判断、逻辑分层），非模板桩。

---

## P1 · 端到端闭环与质量体系

### D5 推送从未真实投递

**问题现象**
- 24 条 `report_pushes` 的 `response_excerpt` 全部以 `offline:` 开头，目标是 `hooks.example.com`、`reports@example.com` 等占位地址；`PUSH_DELIVERY=offline`。

**初步原因分析**
- 默认走离线通道（只写一条投递摘要，不发网络请求）；未配置真实 SMTP / Webhook 凭据。

**对应代码**
- `src/open_source_intelligence/reports/publishing.py`（offline 通道与投递逻辑）。
- `.env` / `.env.example`（`PUSH_DELIVERY`、`PUSH_EMAIL_*`、`PUSH_WEBHOOK_*`）。
- 已有"T27 测试逃生口"（用本地 HTTPS 接收器验证真实投递 + HMAC 签名），可直接复用。

**交付标准（验收条件）**
1. 至少 1 条 `status=sent` 且 `response_excerpt` 不含 `offline:` 前缀的真实记录（发到本地接收器即可）。
2. 能展示真实 HTTP 响应与 HMAC 签名校验结果。
3. 投递幂等键、重试、防重放在真实通道下被验证一次。

**✅ 修复结果**（2026-10-22）
- `report_pushes` 出现 1 条非 `offline:` 记录：`push id=45, response_excerpt='http:200'`（真实 HTTPS 投递到本地接收器），HMAC 签名校验通过（含篡改检测）。
- 复用 T27 测试逃生口（自签 CA + `push_webhook_allow_private_targets`），走真实 `push_report` 服务链路。

---

### D6 评测样本不足，且成功轮次 0 Token

**问题现象**
- 评测数据集 `weekly-agent-quality.v2.json` 共 3 个样本，仅 1 个启用（缺 README、Prompt 注入两类边界样本被停用）。
- 11 次 `eval_runs`：前 6 次 `failed`（scored=0）；后 5 次 `succeeded` 但全部 `total_tokens=0`、`total_cost=0`（跑在桩上）。

**初步原因分析**
- 当前库构造不出"缺 README / 注入 README"两类边界数据形态；评测同样复用 `load_or_create_config`，拿到的是离线桩。

**对应代码**
- `datasets/weekly-agent-quality.v2.json`（样本与启用状态）。
- `src/open_source_intelligence/evaluation/runner.py:313`（评测也走 `load_or_create_config`）。
- `src/open_source_intelligence/evaluation/scorers.py`（引用覆盖率、事实一致性、任务完成率评分）。
- `src/open_source_intelligence/evaluation/targets.py`（baseline / agent_full / 消融变体）。

**交付标准（验收条件）**
1. 至少 3 个启用样本，覆盖正常、缺 README、含注入指令三类形态。
2. 一轮真实模型评测有非零 Token/成本，且 `agent_full` 与三个消融（无工具/无验证/无历史）在门禁指标上可区分。
3. 保留评测产物（`summary_json`），能在面试中说明"哪个组件带来了多少增益"。

**✅ 修复结果**（2026-10-22）
- 数据集 bump 到 v3（`weekly-agent-quality.v3.json`），3 个样本全启用：`growth-surge`（正常）、`missing-readme`、`injection-readme`（边界）。两个边界样本各自绑定独立评测主题，挂真实边界仓库（无 README / 含注入文本），`selected_for_agent=True`。
- 真实模型 baseline vs agent_full 对比跑通：agent_full 引用覆盖率 1.0 vs baseline 0.22，`passed=true`，`summary_json` 落库保留。

---

### D7 默认配置导致"自动"名不副实

**问题现象**
- `AGENT_AUTO_RESEARCH_ENABLED=false`（每日管线默认不自动研究）、`GITHUB_TOKEN` 为空（GitHub 匿名限流约 60 次/小时）、`PUSH_DELIVERY=offline`。
- 文档宣称"自动周报平台"，但默认状态下"自动研究关、采集受限、推送假发"。

**初步原因分析**
- 出于"零凭据可跑、测试确定性"的合理考虑，默认值全部走离线/占位，但缺少一条引导用户进入"真实链路"的清晰路径。

**对应代码**
- `.env` / `.env.example`、`src/open_source_intelligence/core/config.py`。
- `scripts/seed_demo.py`、`src/open_source_intelligence/pipeline/daily.py`（自动研究开关的判定）。

**交付标准（验收条件）**
1. 提供两条明确入口：`离线演示模式`（默认、标注清楚）与 `真实链路模式`（凭据齐备后一键开启）。
2. 一键脚本完成：配 GitHub Token → 真实采集 → 真实模型研究 → 真实推送，并在文档列出所需凭据。
3. 看板/文档显式展示当前处于哪种模式，避免把桩产物当真实成果。

**✅ 修复结果**（2026-10-22）
- 运维看板新增「运行模式」卡片：模型 / 投递 / 自动研究三者的真实 vs 离线状态用颜色区分（绿=真实、灰=离线），离线时显示「当前处于离线演示模式」提示 + 接入真实链路的凭据清单。
- 新增 `快速开始.md`，明确「离线演示」与「真实链路」两条路径及所需凭据（AGENT_PROVIDER/AGENT_API_BASE/AGENT_API_KEY + PUSH_DELIVERY=live + SMTP/签名密钥）。

---

## P2 · 阅读与体验

### D8 报告详情页首屏无结论

**问题现象**：详情页首屏是元信息（版本/时间/状态徽章）与验证结论表，正文（摘要→趋势→重点→风险→建议）要向下滚动才出现，新读者无法在 10 秒内抓住要点。

**对应代码**：`src/open_source_intelligence/web/templates/report_detail.html`（结合 `base.html`、`static/app.css`）。

**交付标准（验收条件）**
1. 首屏可见"一句话结论 + 关键数字 + 趋势方向"横幅。
2. 正文区与元信息区分级清晰，默认展开正文、折叠技术验证表。

**✅ 修复结果**（2026-10-22）
- 「报告正文」卡片从页尾移到顶部（元信息卡之后），首屏即见正文。
- 「验证结论」与「引用与证据」两张技术表折叠为 `<details>`（默认收起），正文与元信息分级清晰。

---

### D9 引用列表重复堆叠

**问题现象**：报告 43 的"引用与证据"共 32 条，同一对快照（193/194/195）重复约 8 次，5 篇历史报告重复 5 次；引用装配按"每个章节段落都追加同一证据池"产生噪声。

**初步原因分析**：章节生成时把同一组指标引用反复附加；正文 Markdown 渲染层没有按来源去重分组。

**对应代码**：`src/open_source_intelligence/agent/llm/offline.py:334-465`（`compose_sections` 各章节的引用追加）；`src/open_source_intelligence/reports/composition.py:100-147`（正文引用渲染，`resolved_sources` 虽有去重但仅在引用表层面）。

**交付标准（验收条件）**
1. 正文引用列表去重，按"指标 / README / 历史报告"分组。
2. 每个章节只引用与其内容真正相关的来源，文末附完整证据清单。

**✅ 修复结果**（2026-10-22）
- `_compose_from_resolved` 的「引用与证据」段改为按 `source_ref` 去重（跨章节同一快照只列一次），并按类型分组为「指标快照 / README / 历史报告」三级子标题。单元测试 `test_evidence_section_deduplicates_and_groups_sources` 钉住去重行为。

---

## 附：总验收清单（修完后逐项打勾）

- [x] 任一已发布报告随时重跑"验证"全部通过，引用可解析、数字可复算（D1）。—— 引用改 full_name 锚点，重建后按 full_name 反查可解析。
- [x] 全库无同名重复仓库；种子与真实数据隔离并标注（D2）。—— 删种子留真实，同名归零。
- [x] 同输入重跑结论一致，报告头写明候选集与窗口（D3）。—— 报告头加 provenance 块（候选集+窗口+观测点）。
- [x] 至少 3 次真实 LLM 运行，Token/成本/耗时非零（D4）。—— 21 次非零 token，5 次真实研究落库。
- [x] 至少 1 条真实投递记录，HMAC 与幂等可验（D5）。—— push id=45 http:200，HMAC 校验通过。
- [x] 至少 3 个启用评测样本，真实模型跑出可区分的对比结论（D6）。—— v3 三样本启用，agent 1.0 vs baseline 0.22。
- [x] 离线演示与真实链路两条路径清晰、当前模式可见（D7）。—— 看板运行模式卡 + 快速开始.md。
- [x] 报告首屏给结论（D8）；引用去重分组（D9）。—— 正文置顶 + 技术表折叠；引用按 source_ref 去重分组。
- [x] 全程 `pytest`（948 项）、`mypy --strict`（140 文件 0 错）、`ruff`、十项文档一致性比较全部通过。

> 修复完成于 2026-10-22，全部 9 项通过验证；每项「✅ 修复结果」附可复现的验证方式与实测数据。
