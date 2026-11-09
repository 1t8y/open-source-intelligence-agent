# 数据回填（Backfill）协议
> 层级：详细规格（展开基线）｜职责：回填的分页、限流、续传与并发守卫｜冲突时以三份根文档为准

历史回填是**可中断、可续传的一次性走查**，以及一条被它喂饱的完整周报链路。本文只描述回填本身：分页语义、限流语义、断点续传、并发守卫、失败分类、CLI 用法与验收口径。它不重复 `ARCHITECTURE.md` §9.7 的设计说明，只把"为什么必须这样做"写清楚，任何改动回填行为的提交都要同步这一份。

## 1. 范围与不做的事

回填**只做采集与入库**：把某个数据源分页搜索的历史仓库，按页落进 `repositories` 与 `repo_snapshots`。它**不**触发研究、不合成报告、不发布、不推送。端到端演示里那部分由既有的每日管线（`pipeline/`）、Agent（`agent/`）、报告链路（`reports/`）与出站（`notifications/`）负责——回填只保证它们有数据可读。

回填**没有 HTTP 面**。触发路径只有两条：`backfill/cli.py` 与 `workers/tasks.py:BACKFILL_TASK`（Celery）。`api` 层不注册任何回填端点（`INTERFACES.md` §3）。

`BACKFILL_TASK` **不进 Celery beat**：回填是一次性、昂贵、且直冲共享公共 API 的操作（当天第一次就免费，之后每次都要间隔）。放进周期表会让它每天自动开一轮对公共 API 的持续压力，而调度回填应该是一个人的决定，不是一个定时器的决定。

## 2. 分页语义：由源决定，不由条目数推断

一套分页协议要能同时套住两个源，而这两个源的分页方式**根本不兼容**：

| 源 | 分页参数 | 总数 | 终止判据 |
|---|---|---|---|
| GitHub Search | `page` / `per_page` | 响应体有 `total_count` | 响应头 `Link` 里**没有 `rel="next"`**（RFC 8288） |
| Hugging Face 镜像（`hf-mirror.com/api`） | `skip` / `limit` 偏移 | 无 | **返回不足一页**（偏移法的唯一信号） |

关键判断是：**`has_more` 必须由源自己说，不能从"这一页拿满了吗"推**。两者只是偶然一致：

- GitHub 走到 **1000 条搜索上限**后，仍然返回完整且不变的 `total_count`，只是不再宣告 `rel="next"`。若按"条目数够不够"判断，游标会继续往后问一个源已经拒绝服务的页码。
- 偏移法的源会在最后一页给出一个**未满**的页。若按"还有没有总数余量"判断，源根本没有总数可看。

所以 `CollectedPage.has_more` 是适配器的结论，不是 walker 的猜测。终止条件只有一个：`has_more is False`。判错边界意味着要么截断一段历史，要么白花一次请求。

### 统一抽象

```python
class PaginatedRepositoryCollector(RepositoryCollector, Protocol):
    def collect_page(self, *, page: int, per_page: int) -> CollectedPage: ...
    def describe_query(self) -> dict[str, Any]: ...
```

- `collect_page(page, per_page)` 回答"给我第 P 页"，而基类的 `collect(limit)` 回答"给我 N 个仓库"。可续传的走查需要的是前一个问题：它持久化的**游标就是一个页码**，续传 = 用更大的页码再问一次，而不是重放前面的页。
- `describe_query()` 让作业能记录"它到底问了什么"。从游标反推查询是猜测；只有适配器知道自己在走哪个搜索。**一个定义**，而不是在作业行里存一份会和真实请求漂移的副本。
- T4/T5 既有契约 `collect(limit)` **不变**：回填是新增能力，不是改口径。

`MAX_PER_PAGE = 100` 是两个源都认的最大页；`MAX_TARGET_PAGES = 100` 是走查的页预算上限（GitHub 拒绝对 1000 条以后分页，更大的预算只会买来源不肯给的请求，也让 `next_page` 绝不会指到没有任何源会回答的页码）。

## 3. 限流语义：请求起始间隔

限流器**不是令牌桶**。令牌桶允许先攒一笔突发额度再补给，而回填的**全部风险就是持续负载**——20 个问题连着问一个公共 API。所以策略取最简单也最正确的那个：任何时刻都不超过配置速率。

- `RateLimiter(per_minute=N)` 强制**相邻两次请求的起始时间**至少间隔 `60 / N` 秒。
- **第一次免费**：`wait()` 第一次调用直接放行、返回 `0.0`。
- 之后每次释放时刻按 `last_release + interval` **推进**，而不是回读当前时钟。回读会让一个睡过头的调用方"攒下信用"抵扣下一次请求——那正是限流要防的突发。同时取 `max(target, clock())`，不让一个真的睡慢了的调用方被双重计费。

时钟与 sleeper 都是**注入**的（`clock=time.monotonic`、`sleep=time.sleep`），理由单刀直入：一个"只能靠等待来证明"的限流器，会让此后每次测试都变慢，而"靠 sleep 来证明"的测试没人会留着。

`run_backfill(..., limiter=None)` 时，限流器**按数据源自己的 `rate_limit_per_minute` 现建** —— 数据源行上配置的预算，就是实际执行的预算，不在第二个地方再写一遍。

## 4. 断点续传：作业行就是检查点

`backfill_jobs` 的每一列都参与续传，`next_page` / `pages_completed` / `items_collected` / `attempt` / `heartbeat_at` 全在表里——进程会死，列不会。

**每页提交一次**是整件事的重点，而不是优化。一个走了 20 页里第 7 页后被杀的走查，留下 `next_page = 8`；下一次从 8 开始。若只在成功时提交一次，每一次中断都变成完整重放。

**同一页的事务边界**：当页仓库（`collectors/service.ingest_items()`，不提交）与游标推进（`backfill/jobs.py:advance_job()`，提交）在**同一事务**里完成。入库本身按 `(source_id, provider_repo_id)` 幂等，重放不会产生重复行——但同事务让崩溃窗口**无害**而非"可恢复"：要么整页连游标一起落盘，要么都不落盘，绝不会出现"仓库进去了游标没动"（重抓一页）或"游标动了仓库没进去"（漏数据）。**游标动了，就是有一页落地了。**

作业生命周期与状态机：

```text
pending ──claim──> running ──┬─ 走完 / 源耗尽 ──> succeeded
                             ├─ 某页失败 ───────> failed（游标停在失败页，可续传）
                             └─ cancel_job ─────> cancelled
```

`TERMINAL_JOB_STATUSES = (succeeded, failed, cancelled)`。注意 **`failed` 是可续传的**——它记录的是"这次尝试在某页停下了"，不是"这个作业没救了"。**`succeeded` 不可续传**：游标已经越过所有目标页，续传要么重放已过的页，要么冒领从未取得的进度，`BackfillNotResumable` 直接拒绝而不是猜你想干嘛。

`BackfillJobSummary.resumable` 的口径是 `status != succeeded and next_page <= target_pages`，CLI 与 worker 输出都带这一位。

## 5. 并发守卫与心跳

**`running` 且心跳未过期的作业不可被再次认领**——`claim_job()` 抛 `BackfillJobBusy`。这是**拒绝而非抢占**：让第二个走查从同一个游标同时出发，会把一页的写入与另一页的游标搅在一起。

心跳信号是**独立列 `backfill_jobs.heartbeat_at`**，不是复用 `TimestampMixin.updated_at`。原因是一个具体的框架行为：SQLAlchemy 2.0.34 的 Python 侧 `onupdate` 会在**每次 UPDATE** 时覆盖显式赋给 `updated_at` 的值（INSERT 时不覆盖），于是这一列永远只装真实墙钟，钉不住。一个测试控制不了它的时钟的守卫，就是一个没人会测的守卫——这正是 T16 期间修掉的产品缺陷之一。

`STALE_AFTER = timedelta(minutes=15)`：一个 `running` 作业超过 15 分钟没有心跳，另一个走查就可以接管它。判据取 `claim_job(now=...)` 的注入时钟与 `heartbeat_at` 之差。

## 6. 失败分类与运行记录

走查里任何一页失败 → 记下失败页、`attempt += 1`、抛 `BackfillWalkFailed`（异常对象带 `job_id` / `run_id` / `error_code` / `next_page`，让调用方不读库就能报出游标停在哪）。

失败分类沿用 `pipeline/runs.py:classify_error()`：任何 `CollectorError`（源拒绝、重试耗尽）→ **`COLLECTOR_FAILED`**。`STAGE_ERROR_CODES` **故意不为 `backfill` 加成员**：正因不加，"源拒绝了"与"我们自己的写入坏了"（落到 `PIPELINE_UNEXPECTED_ERROR`）才保持可区分。真实的未认证 GitHub 限流（`403`）就落在这里，游标停在 `next_page = 8`、`resumable = true`。

**每次尝试一条 `pipeline_runs` 行**（`run_type = "backfill"`），不是每个作业一条：第 1 次失败、第 2 次续传成功，就该有两条运行记录，`metrics_json.backfill_job_id` 与 `provider` 标明归属。`backfill` 是一个**全局运行类型**（`topic_id` 为 `NULL`），所以加值时要**四处同步**：ORM `run_type_valid` CHECK、ORM `topic_scope_valid` CHECK、迁移里的两条 CHECK、`pipeline/runs.py:GLOBAL_RUN_TYPES`。

每次尝试还写一条 `audit_logs`（`action = "backfill.run"`，`result` ∈ `success`/`failed`，`resource_id` = 作业 id）。

## 7. CLI 用法与退出语义

```powershell
# 起一轮新走查（默认 --provider github，--target-pages 5，--per-page 50）
python -m open_source_intelligence.backfill.cli --action start \
  --provider github --target-pages 12 --per-page 100

# 覆盖数据源自己的限流预算（仅在需要时显式指定）
python -m open_source_intelligence.backfill.cli --action start \
  --target-pages 3 --per-page 4 --requests-per-minute 60

# 从游标续传一个作业（同一份 --provider，页码由作业行给出）
python -m open_source_intelligence.backfill.cli --action resume --job-id 42

# 只看不抓：读作业或列表，完全不碰数据源
python -m open_source_intelligence.backfill.cli --action status --job-id 42
python -m open_source_intelligence.backfill.cli --action list --provider github --job-status failed
```

`--provider` 只有 `github` / `huggingface`，**没有 `all`**：一轮走查覆盖一个源。`--action` 只有 `start` / `resume` / `status` / `list`——动作集照着作业生命周期排，而不是照着 HTTP 方法排。

退出语义：

| 情形 | 结果 |
|---|---|
| 走查成功（走完目标页或源耗尽） | 退出码 0，打印 `BackfillOutcome` JSON |
| 走查中途某页失败 | 非零退出，抛 `BackfillWalkFailed`；作业与运行行已记录失败页 |
| `--target-pages` / `--per-page` 越界 | 非零退出，`BackfillLimitError`（**写行之前**就拒） |
| `resume` 一个已 `succeeded` 的作业 | 非零退出，`BackfillNotResumable`（拒绝而非猜） |
| 作业被另一个走查占用 | 非零退出，`BackfillJobBusy`（带 `held_since`） |
| 选中的源不可分页 | 非零退出，`BackfillUnsupportedCollector` |
| `status` / `list` | 退出码 0，打印 `{"job": ...}` 或 `{"jobs": [...]}`，**不触碰任何 provider** |

`status` / `list` 走 `_read()` 分支，只开数据库引擎，不开 HTTP 客户端——"看一眼进度"不应该产生一次对外请求。

## 8. 验收口径

T16 的验收是一句话：**完成一次历史回填和完整周报生成**。落到可执行断言上是 13 条（`scripts/verify_t16.ps1` 逐条复述 `scripts/t16_mysql_smoke.py` 的 payload）：

1. **分页边界** —— 宽走查（`target_pages` 大于源实际页数）按缺席的 `rel="next"` 停在 `next_page = 源页数 + 1`，`has_more` 在最后一页为假。
2. **中断续传** —— 第 3 页 500 后游标停在 `next_page = 3`、`pages_completed = 2`、`items_collected = 6`；续传只请求 `[3, 4]`。
3. **幂等** —— 同一作业重放不新增仓库行（`(source_id, provider_repo_id)` 唯一）。
4. **页预算** —— 走查严格不超过 `target_pages`。
5. **源预算** —— 实际使用的限流预算 = 数据源行的 `rate_limit_per_minute`。
6. **并发守卫** —— 心跳新鲜时第二次认领被拒；心跳过期后可接管。
7. **拒绝语义** —— 不可分页源被拒；越界预算在写行前被拒；已成功作业续传被拒。
8. **失败分类** —— 源拒绝 → `COLLECTOR_FAILED`，游标保留、`resumable = true`。
9. **周报链** —— `run_daily_pipeline` → `run_research_agent` → `generate_report` → `verify_report` → `publish_or_request_review` → `record_review(approve)` → `publish_due_reports` 全程走真实代码。
10. **状态机** —— 报告经 `draft → ... → published`，所有迁移经过 `reports/state.py`。
11. **CLI** —— `list` / `status` / 越界拒绝 / 已完成续传拒绝的真实子进程退出码。
12. **readback** —— **全新会话**重读：作业终态、续传后游标的 `attempt`、续传两条运行记录的状态序列 `["failed", "succeeded"]`、验证结论 `passed`、审核行、版本行、引用行。
13. **确定性** —— 连续两次运行，除时钟与自增 id 外逐字一致。

一键验证：

```powershell
./scripts/verify_t16.ps1

# 本机 Docker 引擎起不来时的等价路径：同一份冒烟模块 + 同一套断言，跑在真实 SQLite 文件上
./scripts/verify_t16.ps1 -SqliteFixture
```

## 9. 文档与实现对照

| 事实 | 出处 |
|---|---|
| 作业表结构与约束 | `docs/data-model.md` §5 `backfill_jobs`、§9 不变量、§11 T16 写入方 |
| 运行类型四处同步 | `docs/data-model.md` §5、`INTERFACES.md` §7 |
| 模块边界与 Worker 接口 | `INTERFACES.md` §4（`collectors` / `backfill`）、`ARCHITECTURE.md` §9.7 |
| CLI 退出语义 | 本文 §7、`INTERFACES.md` §8 |
| 测试 | `tests/backfill/`、`tests/collectors/test_paging.py`、`tests/collectors/test_limiter.py` |
| 端到端冒烟 | `scripts/t16_mysql_smoke.py`、`scripts/verify_t16.ps1` |
