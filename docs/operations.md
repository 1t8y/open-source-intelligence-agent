# 运维手册（Operations）

这份文档面向**把平台跑起来并让它持续产出的人**。它只写操作：每个动作的准确命令、成功时应该看到什么、失败时先查哪里，以及哪些动作是危险的。设计理由（为什么不这样做）不在这里重复，见 `ARCHITECTURE.md` 与各模块的 `docs/*.md`。

**本文的命令都在运行中的五服务栈上逐条实跑过**（2026-10-16，Compose + 真实 MySQL + 真实 GitHub / HF 镜像），输出按原样贴出并去掉无关字段。少数没有实跑的条目在正文里**显式标注**并写明原因 —— 一份没人跑过的运维手册等于一份没人验证过的断言。实跑用的是 `--env-file .env.example`（本地样例口令），命令形状与本文给出的完全一致。

一个前提教训值得先看：**镜像里装的是包，不是挂载的源码**。改完代码不重建，容器里跑的还是上一次构建的版本 —— 实测那份 2026-10-15 17:33 构建的镜像上，`/ui/topics`、`/ui/pushes`、`/ui/audit` 与 `/api/v1/topics` 全是 **404**，而它们对应的代码早已提交。见 §2 第 2 步。

## 1. 这套部署的形状，以及"自动"到哪一步为止

| 容器 | 职责 |
|---|---|
| `api` | HTTP 面（JSON API `/api/v1`）与管理界面（`/ui`）；**不发 SQL 之外的业务逻辑**，读写都走同一套 service |
| `worker` | Celery worker：执行被派发的任务（采集、日报、回填、发布扫描、推送重试） |
| `beat` | Celery beat：**定时器**，决定什么会自己发生 |
| `mysql` | 唯一数据源（`mysql-data` 卷） |
| `redis` | Celery broker |

`beat` 的周期表里只有四项（`workers/celery_app.py:build_beat_schedule`）：每分钟的健康检查、每 `PUBLISH_SWEEP_MINUTES` 一次的到期发布扫描、每 `PUSH_RETRY_INTERVAL_MINUTES` 一次的推送重试、以及每天 `PIPELINE_SCHEDULE_HOUR:PIPELINE_SCHEDULE_MINUTE`（默认 02:00）的**确定性流水线**。

**关键事实：每天的定时任务只做到"排名 + 预热仓库正文"。** `run_daily_pipeline` 的四个阶段是 collect → snapshot → rank → contents（`pipeline/daily.py`），`contents` 把入选候选的 README 抓进 `repository_contents` 缓存（研究运行读的就是这份缓存，见 §7）；它**不启动研究 Agent**；`workers.tasks.run_research_agent` 已注册但**不在 beat 表里**（`ARCHITECTURE.md` §9.3 记了这条决定，`tests/workers/test_agent_task.py:57` 用断言钉住它）。所以：

> **"界面里没有新报告"的第一嫌疑不是"坏了"，而是"今天还没人跑研究"。** 报告生成、审核、发布、推送这一半都是**人工触发**的。§7 与 §8 是这一半的入口。

## 2. 第一次启动

```bash
# 1. 环境文件。compose 的 ${VAR} 替换读的是它，应用自身的 Settings 也从这里取。
cp .env.example .env
#    必须改：MYSQL_PASSWORD / MYSQL_ROOT_PASSWORD / DATABASE_URL 里的口令 / SECRET_KEY
#    想接真实模型与真实投递再改：AGENT_PROVIDER、AGENT_API_BASE、AGENT_API_KEY、PUSH_DELIVERY 及 SMTP/签名密钥

# 2. 起服务。注意两点：
#    (a) --env-file：compose 文件在 infra/ 下，compose 默认去 *项目目录* 找 .env，也就是
#        infra/.env —— 不写 --env-file 就会拿到一整套空变量（实测会打印一堆
#        'The "DATABASE_URL" variable is not set'，随后 mysql 起不来）。
#    (b) --build：镜像里装的是包，不是挂载的源码。改了代码不重建，容器里跑的还是旧版本
#        —— 实测旧镜像上 T19/T20 的页面与端点直接 404。
docker compose --env-file .env -f infra/docker-compose.yml up -d --build --force-recreate --remove-orphans

# 3. 建表。镜像不自动迁移，迁移永远是一条显式的命令。
docker compose --env-file .env -f infra/docker-compose.yml exec -T api alembic upgrade head
```

第 3 步在已迁移的库上是一条幂等空操作，实测输出（只有这两行 INFO，没有 `Running upgrade`）：

```text
INFO  [alembic.runtime.migration] Context impl MySQLImpl.
INFO  [alembic.runtime.migration] Will assume non-transactional DDL.
```

```bash
# 4. 健康检查
curl -s http://127.0.0.1:8000/health
# {"status":"ok","service":"open-source-intelligence-agent","version":"0.1.0"}

docker compose --env-file .env -f infra/docker-compose.yml ps
```

**重建前后对照**（实测，用来判断"我是不是在看旧版本"）：镜像构建时间晚于 `git log -1` 才对得上。旧镜像上八个界面路由里只有 `login` / `reports` 存在，其余 404；重建后全部变成 303（未登录跳登录页，**说明路由在**）：

```text
旧镜像（构建于 2026-10-15 17:33，HEAD 已是 2026-10-16 22:04）
  /ui/login 200   /ui/reports 303   /ui/topics 404   /ui/pushes 404   /ui/audit 404
  GET /api/v1/topics -> 404
重建后（22:36 构建）
  /ui/{login,reports,topics,pushes,runs,evaluations,dashboard,audit} -> 200 / 303
  GET /api/v1/topics -> 401   （未带 token：路由存在，是鉴权拒绝）
```

未登录时管理界面是"能打开登录页、其余一律 303 回登录页"（实测 `200` / `303 → /ui/login?next=%2Fui%2Freports`）：这是**功能正常**，不是权限故障。

## 3. 账号与角色（后端操作）

账号**没有界面**：这是有意的取舍（`docs/api-contract.md` §6「运维配置不从界面管理」），只有下面两条路。

**第一个管理员**（也是唯一有 CLI 的建号路径）：

```bash
# 密码只能走环境变量 —— 不接受 argv，因为命令行会留在 shell 历史与进程列表里。
docker compose --env-file .env -f infra/docker-compose.yml exec -T \
  -e BOOTSTRAP_ADMIN_PASSWORD='<一次性强口令>' api \
  python -m open_source_intelligence.security.bootstrap --username admin --display-name "运维管理员"
# bootstrap_admin_ready=admin
```

对已存在的同名账号再跑一次是**提升与重置**：密码被重设、`status` 置为 `active`、`data_scope` 置为 `team`、缺 `ADMIN` 角色就补上；每次都留一条 `auth.bootstrap_admin` 审计行。

**其它角色**（`EDITOR` / `VIEWER`）没有 CLI，用下面这段（实跑通过，`created 74 __t22_drill__editor team`）：

```bash
docker compose --env-file .env -f infra/docker-compose.yml exec -T -e NEW_PASSWORD='<一次性强口令>' api \
  python - <<'PY'
import os
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence.session import create_database_engine, create_session_factory
from open_source_intelligence.security.passwords import hash_password
from open_source_intelligence.security.service import create_user

factory = create_session_factory(create_database_engine(get_settings().database_url))
with factory() as session:
    user = create_user(
        session,
        username="analyst",
        password_hash=hash_password(os.environ["NEW_PASSWORD"]),
        display_name="分析师",
        role_codes={"EDITOR"},     # 角色码：ADMIN / EDITOR / VIEWER
        data_scope="team",         # team 才能读 TEAM 可见范围的报告；personal 只能读自己的 PRIVATE
    )
    session.commit()
    print("created", user.id, user.username, user.data_scope)
PY
```

角色能做什么：`ADMIN` 全权；`EDITOR` 可写报告（验证/审核/发布/推送/编辑正文）与主题的**读**，可看看板、推送记录、评测；`VIEWER` 只读，且读不到 `TEAM` 范围的报告。三个角色的界面菜单会**自动少项**，同时直接访问也被拒 —— 菜单与路由是同一个常量生成的（`web/router.py:NAV_GROUPS`）。

## 4. 主题（唯一有界面的写配置面）

主题决定"盯哪些仓库"，也就是决定下一次每日运行花掉多少模型预算与公共 API 配额，所以**写只有 `ADMIN`**，且这是界面上唯一能改配置的地方。

- 界面：`/ui/topics`（新建 / 编辑 / 启停 / 删除）
- API：`GET/POST /api/v1/topics`、`GET/PATCH/DELETE /api/v1/topics/{topic_id}`
- **触发研究不在 API 上**：`POST /topics/{id}/run` 刻意不存在（消耗模型预算，触发权属调度器与操作员 CLI）

在**当前构建**的实跑实例上（先 `POST /api/v1/auth/login` 取 token，再带 `Authorization: Bearer`）：

```text
POST   /api/v1/topics            -> 201 | id 40 | mode MANUAL | enabled True | created_by 75
GET    /api/v1/topics?q=__t22_drill__  -> 200 | total 1
PATCH  /api/v1/topics/40         -> 200 | min_stars 50
DELETE /api/v1/topics/40         -> 200 | {"deleted": true, "unlinked_candidate_count": 0}
GET    /api/v1/topics?q=__t22_drill__  -> 200 | total 0
```

两个容易踩的点：`q` 是**字面子串**（`%` 与 `_` 不是通配符）；删除一个**有历史引用**的主题会被拒 `409 TOPIC_IN_USE` 并点名引用它的表 —— 那种主题应当**停用**而不是删除。

字段语义（留空 = **不限制**，不是"什么都不收"）：

| 字段 | 含义 |
|---|---|
| `min_stars` | 候选仓库的星标下限 |
| `window_days` | 观察窗口；排名按这个窗口内的快照算净增 |
| `max_candidates` | 送进研究 Agent 的候选上限（直接决定模型预算上限） |
| `languages` / `keywords` | 逗号分隔的过滤；两者都为 NULL 时才是不限制 |
| `publish_mode` | `AUTO` / `AUTO_WITH_DELAY` / `MANUAL`，见 §8 的发布策略 |
| `enabled` | 只有 `enabled=True` 的主题会被每日流水线读到；**没有启用主题，采集照跑但排名为空，也就不会有任何报告** |

## 5. 推送目标白名单（后端操作）

白名单是报告出站的**唯一**允许列表：目标不在这张表里，推送接口就等于可以往任何地址发。界面只在管理员的白名单卡里**显示**它，不提供创建入口。

```bash
docker compose --env-file .env -f infra/docker-compose.yml exec -T api python - <<'PY'
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence.session import create_database_engine, create_session_factory
from open_source_intelligence.persistence.models.notifications import PushTarget

factory = create_session_factory(create_database_engine(get_settings().database_url))
with factory() as session:
    session.add(PushTarget(
        name="ops-email",
        channel="email",              # email / webhook
        destination="ops@example.com",
        enabled=True,                 # 停用中的目标不出现在推送下拉里，但会出现在管理员的白名单卡里
        allow_private=False,          # 两个放行开关默认关：新建的目标在有人明确放开前
        allow_confidential=False,     # 不会收到私有或机密报告
        description="周报投递",
    ))
    session.commit()
    print("target written")
PY
```

三个开关的作用：`enabled` 决定**能不能选**；`allow_private` / `allow_confidential` 决定**选了之后这条线路能不能送敏感级别的报告**（默认关，是"最小权限"的默认值）。

**投递本身还有一道开关**：`PUSH_DELIVERY=offline`（默认）走一个确定性、无网络的传输，任何环境都能跑；`live` 才会真的连 SMTP / 发 Webhook，且**缺凭据时在任何投递行落库之前就被拒绝**（所以"配置错了"不会留下半条记录）。

## 6. 触发采集、排名与正文缓存（确定性半段）

```bash
# 只跑一次采集（一个源一批）
docker compose ... exec -T api python -m open_source_intelligence.collectors.cli --provider github --limit 1
# {"errors": {}, "provider_counts": {"github": 1}}

# 完整确定性流水线：采集 → 快照 → 排名 → 仓库正文缓存（每个启用主题一次）
docker compose ... exec -T api python -m open_source_intelligence.pipeline.cli --action daily --provider github --limit 3

# 只刷新正文缓存（排名结果已在库里时用这个；--force 忽略当天的幂等窗口）
docker compose ... exec -T api python -m open_source_intelligence.pipeline.cli --action contents --provider github
docker compose ... exec -T api python -m open_source_intelligence.pipeline.cli --action contents --provider github --force
```

其它动作：`--action snapshot`（只快照）、`--action rank`（只排名）、`--action all`、`--action runs`（看运行账本，见 §12）。

**`contents` 是报告链路的前置条件，不是可选项。** 研究运行读 README 的唯一途径是 `repository_contents` 这张缓存表（工具**不会**在运行中间去抓网络），所以缓存空着时研究运行必然停在 `AGENT_EVIDENCE_INCOMPLETE`、`reports generate` 必然失败于 `readme source not found` —— §11 记的正是这两个症状。它抓给谁由排名决定：**启用主题下 `selected_for_agent=true` 的候选**，按 `(topic_id, rank)` 去重后取前 `PIPELINE_CONTENT_LIMIT`（默认 20）个。抓取结果分五类计数写进运行行：

| 计数 | 含义 |
|---|---|
| `fetched` | 写了新正文（内容变了或首次抓到） |
| `skipped` | 当天已确认过（`fetched_at >= 业务日起点`），没有发起请求 |
| `missing` | 源明确答复"没有 README"（GitHub `404`），不是失败 |
| `unsupported` | 该候选的 provider 没有内容能力（当前只有 GitHub 有，见下） |
| `failed` | 源拒绝或超时，`metrics.failures` 里带仓库 id 与脱敏后的原因 |

`failed` 大于零而其余四类之和为零时该运行记为 `failed`，部分失败记为 `partial`；一个仓库抓不到**不会**终止当天批次。刷新按业务日的**时区起点**做幂等窗口，所以同一天重复跑 `daily` 不会重复请求（`--force` 可越过）。

当前的 provider 覆盖面：**只有 GitHub 实现了 `fetch_readme`**（Hugging Face 的适配器没有实现这个能力）。所以候选里若有 HF 仓库，它们会记 `unsupported`，其 README 缓存会一直为空 —— 研究运行读它们时拿到的是 `TOOL_NOT_FOUND`，这是"未缓存"的诚实结果，不是缺陷。

`--date` 是**业务日**（默认按 `APP_TIMEZONE` 推今天）：快照、排名、正文缓存的幂等窗口、以及"这个业务日是否已经跑过"的判断都以它为准。补跑昨天用 `--date 2026-10-15`。

**采集是可用的**（实测 GitHub `api.github.com` 与 HF 镜像两条都能取到数据，并更新 `data_sources.last_success_at`）。`provider_errors` 非空时才需要看这一源的凭据与限流。

## 7. 研究 Agent（人工触发）

```bash
# 跑一次研究：读排名结果里"入选"的候选，产出报告草稿
docker compose ... exec -T api python -m open_source_intelligence.agent.cli \
  --action run --topic-id 14 --max-candidates 3

# 看运行账本 / 看某一次运行的全部步骤与工具调用
docker compose ... exec -T api python -m open_source_intelligence.agent.cli --action runs --run-limit 5
docker compose ... exec -T api python -m open_source_intelligence.agent.cli --action runs --run-id 28
```

实测输出（run 30，离线桩，节选）：

```json
{"run": {"agent_run_id": 30, "status": "completed", "error_code": null, "candidate_repo_ids": [100, 99, 101],
 "evidence_count": 9, "tool_call_count": 15, "report_draft_id": 14, "section_count": 6, "guard_triggered": false,
 "verification": {"covered_repo_ids": [99, 100, 101], "missing_repo_ids": [], "content_repo_ids": [99, 100],
                  "has_prior_baseline": true, "sufficient": true, "reason": "evidence base complete for an internal draft"}}}
```

`--window-start` / `--window-end` 覆盖窗口（ISO 8601）；`--max-candidates` 覆盖主题的候选上限，**这是控制这一轮模型预算最直接的一个旋钮**。运行级预算在 `AGENT_MAX_LOOPS` / `AGENT_MAX_MODEL_CALLS` / `AGENT_MAX_TOOL_CALLS` / `AGENT_MAX_TOKENS` / `AGENT_MAX_COST`。

⚠️ **`AGENT_MAX_TOKENS` 的默认值 12000 在"缓存有内容"之后偏小。** README 缓存修好之前（T23），读缓存工具每次都失败，工具载荷很短，同一主题的研究跑下来只有几千 token；缓存一填上，证据真实地变长，实测一次运行 `total_tokens` 18753，于是真实模型那次以 `AGENT_BUDGET_EXCEEDED` 收口（`status=needs_review`，草稿仍写完了 6 个章节）。离线桩因为不计 token 不受影响。要跑通"真实模型 + 真实证据"的完整链路，把 `.env` 的 `AGENT_MAX_TOKENS` 提到 **25000 以上**（`AGENT_MAX_COST` 同样要按真实价格核对）。这不是缺陷，是**真实证据的成本第一次被看见**。

**它是人工触发的，不是忘了接。** 这条决定写在 `ARCHITECTURE.md` §9.3：Agent 消耗真实模型预算，自动编排要等报告与发布阶段就绪后再开。⚠️ 那两阶段（T11–T13）已经完成，**但开关还没人开** —— 当前状态是"必须人工触发"，见 §11 第 4 条。

## 8. 报告链路（人工触发）

`reports.cli` 是报告侧的全部动作，顺序即状态机顺序。下表的**状态**列说明这一条在当前部署里能不能走通（T23 修好 README 缓存之后，生成→验证→发布这三步已经实跑过）：

| 动作 | 命令（前缀省略 `docker compose ... exec -T api python -m open_source_intelligence.reports.cli`） | 状态 |
|---|---|---|
| 生成 | `--action generate --agent-run-id <id>` | ✅ 实跑：报告 36（真实模型那次）与 37（离线桩），`{"created": true, "report_id": 37, "source_count": 26, "version": 1}`。**T23 之前这一条硬失败于 `readme source not found: repository_contents:99:readme`** |
| 验证 | `--action verify --report-id <id>` | ✅ 实跑：`{"verdict": "passed", "citation_coverage": 1.0, "unresolved_refs": [], "structure_ok": true, "numbers_ok": true, "sensitivity_ok": true, "sensitive_markers": 0, "reason_codes": []}` |
| 发布 | `--action publish --report-id <id>` | ✅ 实跑：`{"status": "scheduled", "mode": "AUTO_WITH_DELAY", "requires_review": false, "reason_codes": [], "publish_at": "…"}` —— 进了延迟窗口，等 `PUBLISH_DELAY_MINUTES` 到期或 `publish-due` 释放 |
| 审核 | `--action review --report-id <id> --decision {approve\|reject\|request_changes} --reviewer-id <uid> --comment "…"` | ⏸ 未实跑：本部署的主题是 `AUTO_WITH_DELAY`，走不到必须人工 approve 的那条路（`MANUAL` 模式才需要） |
| 到期释放 | `--action publish-due --limit 20` | ✅ 实跑：`{"released": []}`（当时没有到期报告，空操作） |
| 推送 | `--action push --report-id <id> --target-id <target_id>` | ⏸ 未实跑：报告停在 `scheduled`，推送只在 `published` 之后发生 |
| 推送重试 | `--action push-retry --limit 20` | ⏸ 未实跑：同上；且 `push_targets` 里只有一行且为测试目标 |

界面 `/ui/reports` 是同一套动作的点击版。`--action push` 的**渠道取自目标行**，命令行传 `--channel` 不会覆盖它（表单同理）。

发布策略（`PUBLISH_*` 一组配置）：

- `AUTO`：验证通过就发；`AUTO_WITH_DELAY`：先进队列，等 `PUBLISH_DELAY_MINUTES`（默认 60）到期、操作员仍可撤回的窗口过去后才发；`MANUAL`：必须人工 approve。
- 引用覆盖率低于 `PUBLISH_MIN_CITATION_COVERAGE`（默认 1.0）**永远不会自动发布**，转人工审核。
- 到期释放有两个入口：beat 每 `PUBLISH_SWEEP_MINUTES` 扫一次，或 `--action publish-due` / 界面上的「释放到期报告」按钮。**在只有排队、没有到期报告时它是空操作**，不是故障。
- 发布这一步的裁决结果体现在它的返回值与对应的 pipeline run 指标里：`requires_review` 与 `reason_codes`。实测见到的原因是 `AGENT_RUN_NOT_CLEAN` → `resolution: pending_review`（同一类还有 `PUBLISH_POLICY_BLOCKED`）。看到 `requires_review: true` 就是**策略按设计把它交给人了**，不是失败。

## 9. 回填（后端，人工触发）

```bash
docker compose ... exec -T api python -m open_source_intelligence.backfill.cli \
  --action start --provider github --target-pages 1 --per-page 5
```

实测输出（job 6）：

```json
{"status": "succeeded", "job_id": 6, "items_collected": 5, "pages_completed": 1, "next_page": 2,
 "per_page": 5, "provider": "github", "target_pages": 1, "run_id": 179,
 "query": {"q": "topic:llm stars:>100", "sort": "stars", "order": "desc"}}
```

- `--action resume --job-id <id>` 接着走；`--action status --job-id <id>` 看单个作业；`--action list` 看列表。✅ `list` 已实跑；⏸ `resume` / `status` 未实跑（需要一份**未走完**的作业，本轮的作业一页就走完了）。
- 每个源单独走一次（没有 `all`）；`--requests-per-minute` 只能覆盖**这一轮**的起始间隔，不改数据源上的默认预算。
- **作业行就是检查点**：`next_page` / `pages_completed` / `heartbeat_at` 都在行里，所以进程死掉可以续；`STALE_AFTER`（15 分钟）之后别的走查者才能接手，且是**拒绝而不是抢占**。
- **回填不进 beat**：它是对共享公共 API 的一次性昂贵走查，触发权属于操作员。

## 10. 评测（后端，人工触发）

```bash
docker compose ... exec -T api python -m open_source_intelligence.evaluation.cli --action list --limit 5      # ✅ 已实跑
docker compose ... exec -T api python -m open_source_intelligence.evaluation.cli --action import --dataset-file <path> --name <name> --version v1   # ⏸ 未实跑（数据集已在库中）
docker compose ... exec -T api python -m open_source_intelligence.evaluation.cli --action run --dataset-id 1 --target agent_full   # ✅ 已实跑
docker compose ... exec -T api python -m open_source_intelligence.evaluation.cli --action compare --dataset-id 1                   # ✅ 已实跑
```

`run` 的实测结果是 `run_id 6`、3 个用例全 `failed`、`scored_case_count 0`、`tool_call_total 0`；`compare` 同样报 `passed: false` 且五个变体的 `scored_case_count` 都是 `0`。原因见 §11 第 3 条 —— **这是"没测到"，不是"回归"**。

读结果时有三条口径必须知道（都是踩过的）：

- **`passed` 只裁决 `agent_full`**，消融变体只是对照；而且 `scored_case_count > 0` 是判定的**必要条件** —— 全部用例都没跑起来时 `passed=false` 说的是"没测到"，不是"回归了"。
- **未行使的指标是 `null` 而不是 `0`**（`citation_coverage` / `task_completion` / `tool_success_rate` 等）。看到 `null` 先查用例为什么没产出分数，不要当成"0 分"。
- 变体名（`baseline_single_prompt` / `agent_full` / `agent_no_verifier` / `agent_no_tools` / `agent_no_history`）是**契约里的标识符，不翻译**。

当前这个库里 `compare --dataset-id 1` 的实测结果是五个变体全 `failed`、`scored_case_count = 0`，原因见 §11 第 3 条。

## 11. 当前状态与已知缺口（实测，2026-10-16）

四处**实测**出来的缺口，都不是"环境不行"，根因都落在代码里，所以都附了证据行。**T23 修掉了第 1、2 两处**（同一根因的两面），第 3、4 仍开放：

- **缺口 1（已修）**：新增 `contents` 阶段（`pipeline/contents.py`）作为 `repository_contents` 的**唯一生产写入方**，排在 `rank` 之后按候选排名预热缓存。实测：`--action daily` 的 contents run（183）抓到 10/10 个 README，10 行缓存的 `char_length(content)` 上界正好 20000、`source_url` 全为 `https`；同一天再跑 `--action contents`（run 184）为 `skipped=10`/`fetched=0` —— 幂等窗口起作用，没有发起第二次请求。
- **缺口 2（已修）**：`agent/llm/messages.py` 提供 `tool_error_payload` / `is_tool_error_payload` 一对定义，`agent/graph.py` 渲染与 `agent/llm/offline.py:_collect_evidence` 过滤共用同一判据 —— 失败观察不再被当成"读到了内容"。实测：研究运行从 `needs_review`+`AGENT_EVIDENCE_INCOMPLETE`+`sufficient=false` 变为 `completed`+`sufficient=true`（`content_repo_ids=[99, 100]`）；`reports generate` 从硬失败 `readme source not found` 变为成功（报告 36/37），`reports verify` 两份 `verdict=passed`、`citation_coverage=1.0`、`unresolved_refs=[]`。

下表保留 T22 当时实测到的**原始现象与根因**（那是修之前的证据，不改写）：

| # | 现象（原始报错） | 根因 | 影响 |
|---|---|---|---|
| 1 | `reports generate` 直接失败：`readme source not found: repository_contents:99:readme` | `repository_contents`（README 内容缓存）在 `src/` 里**没有任何写入方** —— 只有 `scripts/t7..t17_*` 的演练脚本直写；库里现在是 **0 行**。而 `reports/citations.py:85` 在解析 README 引用时找不到行就**硬失败** | 报告草稿一旦引用 README，**生成这一步就走不通** |
| 2 | 研究运行稳定停在 `AGENT_EVIDENCE_INCOMPLETE` / `sufficient=false` / `reason="no cached repository content was read"` | 同一个缓存为空导致 `get_repository_content` 工具**每次都失败**（实测失败码 `TOOL_NOT_FOUND`）；而 `agent/llm/offline.py:_collect_evidence` 把工具失败的载荷（`{"error": {...}}`，也是一个 dict）当成"读到了内容"记进证据 —— **探针实测**：喂进一次失败的读缓存调用后 `evidence.contents = {99: {'error': {'code': 'TOOL_NOT_FOUND', ...}}}`，而对照用例（成功载荷）仍被正常吸收。于是模型据此**引用了它从没读到的 README**（实测草稿引用的正是 `repository_contents:99:readme`，与那次失败的 repo_id 一致） | 于是 1 与 2 是同一根因的两面：**只修一面另一面照样发作** —— 只补缓存写入方，失败载荷仍会在缓存为空时把生成带崩；只修证据吸收，报告则只剩指标证据、`sufficient` 仍为 false |
| 3 | 评测五个变体全 `failed`、`scored_case_count = 0` | 逐条结果是 `EVAL_TOPIC_NOT_FOUND`：数据集里写的是夹具主题名（`AI Agent`），而这个库里只有 `sweep-llm-agent-topic` | 评测链路本身没坏，是**数据集与部署对不上**；改名或更新数据集即可 |
| 4 | 每天定时任务跑完只有采集/快照/排名，永远没有新报告 | `run_research_agent` **不在 beat 表**（`ARCHITECTURE.md` §9.3 的决定：等报告与发布阶段就绪后再开），而那条前置条件（T11–T13）**已经满足** | 自动编排停在排名；研究与报告必须人工触发 |

修法建议（第 1/2 条已在 T23 实施，见 §11 开头；第 3/4 条仍开放）：

- **缺口 1/2（已实施）**：先给"读缓存"补一条生产路径 —— 要么在采集/快照阶段抓 README 落进 `repository_contents`（这也让 `get_repository_content` 有可能成功），要么明确"内容缓存由运维预置"并把 `verify` 的 README 条件降级为可选。同时收紧 `_collect_evidence`：`profile` 与 `content` 两处现在都是 `isinstance(payload, dict)` 就收，**必须排除失败载荷**（`query_repo_metrics` 与 `search_prior_reports` 分支收的是 list，不受影响）。两处都要补**能失败的**回归测试。
- **缺口 3**：把数据集里的主题名改成本部署真实存在的主题，或在导入时校验。
- **缺口 4**：这是一个预算决定，不是代码问题 —— 要么把 `run_research_agent` 加进 beat（并接受每天自动消耗一次模型预算），要么把文档里"等就绪后再开"改成"有意保持人工"。

## 12. 看账本与排查

```bash
# 运行账本（pipeline）：谁在什么时候跑了哪一段，成败与指标
docker compose ... exec -T api python -m open_source_intelligence.pipeline.cli --action runs --run-limit 5
docker compose ... exec -T api python -m open_source_intelligence.pipeline.cli --action runs --run-id 171
# 可按 --run-type / --status / --topic-id 过滤
```

界面上的对应入口：`/ui/dashboard`（运维看板：成本、失败原因、评测结论）、`/ui/runs`（采集与运行台账）、`/ui/audit`（审计台账，**仅管理员**）。

常见症状先查哪里：

| 症状 | 先查 |
|---|---|
| 报告/列表是空的 | 有没有 `enabled=True` 的主题？排名 run 的 `candidate_count` 是不是 0？今天跑过 `--action daily` 吗？ |
| 每天跑完却没有新报告 | 正常 —— 研究不在 beat 里，见 §11 第 4 条 |
| `AGENT_EVIDENCE_INCOMPLETE` | `repository_contents` 是不是空的 —— 跑一次 `--action contents` 再看（§6）。T23 之后这条不该再出现；若仍出现，看 contents 运行行的 `metrics.failures` 里哪些仓库没抓到 |
| `AGENT_BUDGET_EXCEEDED` | `AGENT_MAX_TOKENS` 不够（§7 的 ⚠️）。证据真实变长之后这是真实模型最常见的"跑不完"原因，草稿通常已写完，所以它落在 `needs_review` 而不是 `failed` |
| `EVAL_TOPIC_NOT_FOUND` | 数据集里的主题名与本库主题是否一致（§11 第 3 条） |
| `publish` 后停在 `pending_review` | 看返回里的 `reason_codes`（如 `AGENT_RUN_NOT_CLEAN`）与 `PUBLISH_MIN_CITATION_COVERAGE` |
| 推送 `failed` + 错误码 | 传输失败**不是异常**：接口仍 `200`，失败落在投递行上；看 `error_code` 与 `PUSH_MAX_ATTEMPTS`，重试会推进同一行的 `attempt` |
| 容器起来但接口 500 / 变量为空 | `--env-file` 有没有给对（§2 第 2 步的坑） |
| 想核对"谁改了什么" | `/ui/audit`，或直接查 `audit_logs`；**写操作**才留痕，只读页面渲染被拒不留痕 |

## 13. 危险动作与纪律

- **不要对生产库执行 `down -v`**：它会删掉 `mysql-data` 卷。演练脚本 `scripts/verify_t11..t15.ps1` **要求**全新卷（它们断言绝对计数），`t16` 之后不要求。
- **清理只删能证明是自己的行**：按**字面前缀**匹配（`Column.startswith(prefix, autoescape=True)`），不要用 `LIKE 'x_%'` —— 前缀里的 `_` 是通配符，会连别人的行一起删。
- **不要放宽清理条件去"清干净"**：真实账本上的匿名失败行不是你的，放宽条件的清理就是越权。
- **`verify_*.ps1` 不支持并发**：它们会临时改写仓库里的文档再复原，两个实例会互相消耗备份。顺序跑。
- **同一时刻只跑一份演练脚本**：它们按家族前缀清理自己，两份会互删。
- **改文档/接口/数据模型/配置/依赖之后**先跑 `python scripts/check_docs_drift.py`（十项比较，任何一项不过就退非零）。它是"文档是否同步"的唯一判据。

## 14. 文档与实现对照

| 想知道什么 | 看哪 |
|---|---|
| 架构、模块边界、实施计划 | `ARCHITECTURE.md`（唯一真理源） |
| HTTP 端点、错误码、界面表 | `docs/api-contract.md` |
| 权限与审计边界 | `docs/security-and-privacy.md` |
| 表结构、不变量 | `docs/data-model.md` |
| 指标口径与运行账本 | `docs/observability.md` |
| 回填的分页/限流/续传语义 | `docs/backfill.md` |
| 评测方案与门槛 | `docs/evaluation.md` |
| Agent 工具契约 | `docs/agent-tool-contract.md` |
| 开发流程与门禁 | `docs/development-process.md`、`README.md` |
