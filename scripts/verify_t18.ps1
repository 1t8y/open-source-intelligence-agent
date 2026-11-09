param(
    [string]$Python = 'python',
    [switch]$SqliteFixture
)

$ErrorActionPreference = 'Stop'
$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $projectRoot

# smoke 脚本以 `ensure_ascii=False` 按 UTF-8 打印其载荷，因为演练写入和读取的行的文本是
# 中文。原生命令的 stdout 会按控制台代码页而非 UTF-8 解码，若不这样做，载荷就会以乱码
# 字节的形式到达，`ConvertFrom-Json` 会在字符串中间解析失败。两端都要固定而不是只固定一端：
# `PYTHONIOENCODING` 决定子进程*写入*什么，控制台编码决定 PowerShell *读取*什么。
$previousOutputEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'

$dockerBin = 'C:\Program Files\Docker\Docker\resources\bin'
if (-not (Get-Command docker -ErrorAction SilentlyContinue) -and (Test-Path -LiteralPath $dockerBin)) {
    $env:Path = $dockerBin + ';' + $env:Path
}

function Assert-LastCommandSucceeded {
    param([string]$Label)

    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function Assert-Equal {
    param($Actual, $Expected, [string]$Label)

    if ($Actual -ne $Expected) {
        throw "$Label expected '$Expected' but got '$Actual'"
    }
}

function Assert-True {
    param($Condition, [string]$Label)

    if (-not $Condition) {
        throw "$Label expected a true condition"
    }
}

$composeArgs = @('compose', '--env-file', '.env.example', '-f', 'infra/docker-compose.yml')
$mountArgs = @(
    '-v', "${projectRoot}:/workspace",
    '-w', '/workspace',
    '-e', 'PYTHONPATH=/workspace/src:/workspace/scripts',
    '--entrypoint', 'python',
    'api'
)

# 固件模式的存在是因为本仓库在一台 Docker 引擎并非总能启动的机器上开发。它会用*同一个* smoke
# 模块和下方*同一块*断言，改为针对一个真实的 SQLite 文件而非 MySQL 运行，这样这些检查是真正
# 被执行的，而不只是被写出来。容器模式仍是权威路径：只有它才能同时证明 Compose 接线、
# MySQL 自身的约束语义，以及——正如 T18 所发现的——文档检查器在不同 FastAPI 版本下
# 仍能看到这些路由。
$fixtureDir = Join-Path ([System.IO.Path]::GetTempPath()) 'osia-t18-fixture'
$fixtureDb = Join-Path $fixtureDir 't18_smoke.db'

Write-Host '==> Python tests'
& $Python -m pytest tests -q
Assert-LastCommandSucceeded 'pytest'

Write-Host '==> Ruff lint and format'
& $Python -m ruff check src tests migrations scripts
Assert-LastCommandSucceeded 'ruff check'
& $Python -m ruff format --check src tests migrations scripts
Assert-LastCommandSucceeded 'ruff format --check'

Write-Host '==> Mypy'
& $Python -m mypy src
Assert-LastCommandSucceeded 'mypy'

# ---------------------------------------------------------------------------------------
# Documentation consistency：十项比对，以及它们能够失败的证明。
# ---------------------------------------------------------------------------------------

# 宿主解释器没有安装项目依赖，因此检查器被指向源码树，
# 方式与固件模式把 smoke 指向它相同。
$env:PYTHONPATH = "$projectRoot\src;$projectRoot\scripts"

$driftScript = Join-Path $projectRoot 'scripts\check_docs_drift.py'
$mutatedPaths = @(
    (Join-Path $projectRoot 'docs\api-contract.md'),
    (Join-Path $projectRoot 'docs\data-model.md'),
    (Join-Path $projectRoot 'README.md')
)

Write-Host '==> Documentation consistency'
$checkOutput = & $Python $driftScript 2>&1
Assert-LastCommandSucceeded 'documentation consistency check'
$checkJson = $checkOutput | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1
if (-not $checkJson) {
    throw "the documentation checker produced no JSON payload: $checkOutput"
}
$drift = $checkJson | ConvertFrom-Json
Assert-Equal $drift.failures 0 'the documentation set matches the code'
Assert-True $drift.passed 'every documentation check passes'
# `passed` 只关乎实际运行了的检查。没有它，用 `--only` 缩小过运行范围的人
# 会把结论读成与全量通过相同。
Assert-True $drift.complete 'every comparison ran, so the verdict covers the whole document set'
Assert-Equal $drift.run 10 'all ten comparisons report a result'
foreach ($name in $drift.checks.PSObject.Properties.Name) {
    Assert-True $drift.checks.($name).ok "documentation check '$name' reports no drift"
}
Assert-True ($drift.checks.endpoints.notes[0] -match 'registered paths: \d+') 'the route walk reports how many paths it found'
Assert-True ($drift.checks.'architecture-summary'.notes[0] -match '§7 lists \d+ of \d+ tables') 'the architecture summary reports its table count'
Assert-True ($drift.checks.'status-lines'.notes[0] -match 'last completed task: T\d+') 'the two status lines agree on the last completed stage'
Assert-Equal $drift.checks.'evaluation-targets'.notes[0] 'variants: 5; promotion: agent_full' 'the evaluation variants match TARGET_ORDER'

# 这里最重要的检查不是检查器能通过，而是它*能够*失败。一个永远回答「一致」的
# 比对，并不比它所取代的人工阅读更好，那四个幽灵资源组今天还会留在契约里。所以
# 三份文档被临时改错——API 比对的每个方向一份、数据模型一份、
# 两份状态公告中的一份——并要求检查器在原件放回去之前把三处都报出来。
Write-Host '==> Prove the checker can fail'
# 原件保存在内存里，而不是复制到一个 `.t18backup` 文件。备份文件是一份可能被消费掉的
# 第二副本：本脚本的两个并发运行各自 `Move-Item` 对方的备份，移动失败的那个运行
# 随后完全无法还原——给仓库留下被故意改坏的文档，这比不做这个检查还糟。从字符串
# 还原不会以那种方式失败。（并发运行仍不受支持，因为改动本身是
# 全局的：两个运行都会重写同样的三个文件。）
$originals = @{}
foreach ($path in $mutatedPaths) {
    $originals[$path] = [System.IO.File]::ReadAllText($path)
}
# 以不带字节序标记的 UTF-8 追加：PowerShell 5.1 上的 `Add-Content -Encoding utf8`
# 会写入 BOM，而 Markdown 文件中间的 BOM 是一个游离字符，检查器
# 将不得不被专门教去忽略它。
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
try {
    # 幽灵端点故意只有单个路径段。这正是本任务发现的漂移的形状
    # （`GET /sources`、`GET /topics`），也是更早的「两道或更多
    # 斜杠」启发式会默默放过的形状。
    [System.IO.File]::AppendAllText($mutatedPaths[0], "`n### " + '`GET /t18-not-a-real-endpoint`' + "`n", $utf8NoBom)
    [System.IO.File]::AppendAllText($mutatedPaths[1], "`n### " + '`zz_fake_table`' + "`n", $utf8NoBom)
    # 把 README 状态行里的最后一个阶段号改小，而 INTERFACES.md 保持原样：
    # 两处公告不一致正是状态检查要抓的漂移。进度事实在过程层，比较器核对的
    # 是两份公告是否同步，而不是去读某个任务清单。
    $readme = [System.IO.File]::ReadAllText($mutatedPaths[2])
    $stageNumbers = [regex]::Matches($readme, 'T(\d+)') | ForEach-Object { [int]$_.Groups[1].Value }
    $lastStage = ($stageNumbers | Measure-Object -Maximum).Maximum
    [System.IO.File]::WriteAllText(
        $mutatedPaths[2],
        $readme.Replace("T$lastStage", "T$($lastStage - 1)"),
        $utf8NoBom
    )

    $brokenOutput = & $Python $driftScript 2>&1
    $brokenExit = $LASTEXITCODE
    $brokenText = $brokenOutput -join "`n"
    Assert-Equal $brokenExit 1 'a contract that promises an unregistered endpoint fails the check'
    Assert-True ($brokenText -match 'documented but not registered: GET /t18-not-a-real-endpoint') 'the drift names the endpoint the contract invented'
    Assert-True ($brokenText -match 'documented but not implemented: zz_fake_table') 'the drift names the table the data model invented'
    Assert-True ($brokenText -match 'status lines disagree') 'a stale status announcement is reported as drift'
    # 三项相互独立的比对，检查器必须全部报出而不是在
    # 第一处就停下：一个每次运行只报一个发现的检查器会把五处漂移
    # 变成五次运行。
    Assert-True ($brokenText -match '\[endpoints\]') 'the endpoint comparison is one of the ones that failed'
    Assert-True ($brokenText -match '\[tables\]') 'the table comparison failed as well'
    Assert-True ($brokenText -match '\[status-lines\]') 'the status comparison failed as well'
}
finally {
    foreach ($path in $mutatedPaths) {
        [System.IO.File]::WriteAllText($path, $originals[$path], $utf8NoBom)
    }
}

$restoredOutput = & $Python $driftScript 2>&1
Assert-LastCommandSucceeded 'the documentation set is consistent again after the mutations were undone'
$restoredJson = $restoredOutput | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1
if (-not $restoredJson) {
    throw "the documentation checker produced no JSON payload after restore: $restoredOutput"
}
Assert-True ($restoredJson | ConvertFrom-Json).passed 'the checker passes once the documents are put back'

# ---------------------------------------------------------------------------------------
# T18 保留的两个端点组的运行时证据。
# ---------------------------------------------------------------------------------------

if ($SqliteFixture) {
    Write-Host "==> Prepare the SQLite fixture at $fixtureDir"
    New-Item -ItemType Directory -Force -Path $fixtureDir | Out-Null
    # `_env_file=None` 是关键所在：没有它，固件会读取开发者的 `.env`，
    # 而一旦其中填写了某个供应商配置，就会把这次运行变成一次真实计费且不可复现的运行。
    $sitecustomize = @'
"""Repoint the T18 smoke at a SQLite file, standing in for an unavailable MySQL container."""

from __future__ import annotations

import os

from sqlalchemy import event

from open_source_intelligence.core import config as config_module

_SQLITE_URL = os.environ.get("T18_SQLITE_URL")

if _SQLITE_URL:
    _settings = config_module.Settings(
        _env_file=None,
        database_url="mysql+pymysql://app:secret@127.0.0.1:3306/open_source_intelligence",
        redis_url="redis://127.0.0.1:6379/0",
        secret_key="t18-fixture-secret-key-long-enough-for-validation",
        app_env="test",
        agent_provider="offline",
        push_delivery="offline",
    )
    _settings = _settings.model_copy(update={"database_url": _SQLITE_URL})

    config_module.get_settings = lambda: _settings  # type: ignore[assignment]
    config_module.clear_settings_cache = lambda: None  # type: ignore[assignment]

    from open_source_intelligence.persistence import session as session_module

    _create_engine = session_module.create_database_engine

    def _create_database_engine(database_url: str, *, echo: bool = False):
        engine = _create_engine(database_url, echo=echo)
        if database_url.startswith("sqlite"):

            @event.listens_for(engine, "connect")
            def _enable_foreign_keys(dbapi_connection, _record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        return engine

    session_module.create_database_engine = _create_database_engine  # type: ignore[assignment]
'@
    Set-Content -LiteralPath (Join-Path $fixtureDir 'sitecustomize.py') -Value $sitecustomize -Encoding utf8

    $createSchema = @'
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.base import Base
from open_source_intelligence.persistence.session import create_database_engine

engine = create_database_engine(get_settings().database_url)
Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
print("schema ready:", len(Base.metadata.tables), "tables")
'@
    Set-Content -LiteralPath (Join-Path $fixtureDir 'create_schema.py') -Value $createSchema -Encoding utf8

    $env:T18_SQLITE_URL = "sqlite+pysqlite:///$($fixtureDb -replace '\\', '/')"
    $env:PYTHONPATH = "$fixtureDir;$projectRoot\src;$projectRoot\scripts"

    & $Python (Join-Path $fixtureDir 'create_schema.py')
    Assert-LastCommandSucceeded 'SQLite fixture schema'

    Write-Host '==> Run the T18 documentation-sync smoke on the SQLite fixture'
    $smokeOutput = & $Python (Join-Path $projectRoot 'scripts\t18_mysql_smoke.py')
    Assert-LastCommandSucceeded 'T18 smoke'
}
else {
    Write-Host '==> Validate the Compose file'
    docker @composeArgs config --quiet
    Assert-LastCommandSucceeded 'docker compose config'

    Write-Host '==> Rebuild and start services'
    docker @composeArgs up -d --build --force-recreate --remove-orphans
    Assert-LastCommandSucceeded 'docker compose up'

    # T18 没有新增迁移，因此往返（降级再升级）属于它的前序阶段；它在这里仍需
    # 证明的是：它所运行的 schema 就是迁移构建出来的那一个。
    docker @composeArgs exec -T api alembic upgrade head
    Assert-LastCommandSucceeded 'alembic upgrade head'
    docker @composeArgs exec -T api alembic check
    Assert-LastCommandSucceeded 'alembic check'

    Write-Host '==> Run the T18 documentation-sync smoke on MySQL'
    $smokeOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t18_mysql_smoke.py
    Assert-LastCommandSucceeded 'T18 MySQL smoke'
}

# smoke 脚本驱动的是真实应用，因此其 stdout 同时带有应用日志；
# 载荷是它打印的最后一个 JSON 对象。
$smokeJson = $smokeOutput |
    Where-Object { $_.TrimStart().StartsWith('{') } |
    Select-Object -Last 1
if (-not $smokeJson) {
    throw "the T18 smoke produced no JSON payload: $smokeOutput"
}
$smoke = $smokeJson | ConvertFrom-Json

$catalog = $smoke.catalog
$snapshots = $smoke.snapshots
$audit = $smoke.audit
$documentation = $smoke.documentation
$leftovers = $smoke.leftovers
$expected = $smoke.expected

# ---------------------------------------------------------------------------------------
# Catalog：共享列表与单个仓库的趋势，基于真实行。
# ---------------------------------------------------------------------------------------

# 判据 1：目录是公开的但不是匿名的，且本次 smoke 拥有的切片恰好是三行——正是 `q` 过滤
# 让这个断言在其他演练也写入过的
# 数据库上仍保持精确。
Assert-Equal $catalog.anonymous_status 401 'an anonymous read of the catalog is refused'
Assert-Equal $catalog.total 3 'the catalog slice holds the three drill repositories'

# 判据 2：顺序就是请求的那个顺序，且它不可能碰巧是字母序。
# `beta` / `gamma` / `alpha` 既不是字母序也不是插入序。
Assert-Equal ($catalog.full_names_ordered -join ',') '__t18_drill__/beta,__t18_drill__/gamma,__t18_drill__/alpha' 'the list is ordered by stars, not by name'
Assert-Equal $catalog.paging_overlap 0 'two pages of the same list share no row'

# 判据 3：`provider` 来自联表查询的数据源行而不是常量，这正是
# 三行中有一行落在第二个数据源上的原因。
Assert-Equal ($catalog.providers -join ',') 'github,huggingface' 'the list spans both configured providers'
Assert-Equal $catalog.gamma_provider 'huggingface' 'each row reports the provider of its own source'

# 判据 4：没有 `topic_id` 就从未计算过排序，所以三个排名字段都是
# null。零和 false 会变成对一次从未发生的排序的断言。
$rankValues = @()
foreach ($row in $catalog.unranked_fields) {
    $rankValues += $row
}
Assert-Equal $rankValues.Count 9 'every row reports all three rank fields'
Assert-Equal @($rankValues | Where-Object { $null -ne $_ }).Count 0 'an unsliced list reports no score, rank, or selection rather than zero'

# 判据 5：每个过滤既收窄分页也收窄总数。一个在分页之后才施加的过滤
# 会返回相同的条目和不同的总数，所以总数才是有咬劲的那一半。
Assert-Equal $catalog.language_filter_total 2 'the language filter is case-insensitive'
Assert-Equal $catalog.min_stars_total 1 'the star floor excludes the two smaller repositories'
Assert-Equal $catalog.description_query_total 1 'the text filter reaches the description as well as the name'
Assert-Equal $catalog.future_created_after_total 0 'a first-seen floor in the future matches nothing'
Assert-Equal $catalog.alpha_language $null 'a repository with no language reports null'
Assert-Equal ($catalog.alpha_topics -join ',') 'drill' 'the repository topics round-trip as a list'

# 判据 6：未知的排序、超大的分页、以及不说明自己处于哪个时区的时间戳，都会在边界处
# 被拒绝——最后一项正是 T18 发现的 T12 缺陷，它
# 过去会以查询构造器内部的 500 暴露出来。
Assert-Equal $catalog.unknown_sort_status 422 'an unknown sort is refused rather than silently ignored'
Assert-Equal $catalog.page_size_cap_status 422 'a page larger than the cap is refused'
Assert-Equal $catalog.naive_timestamp_status 422 'a timestamp without a timezone offset is refused at the edge'
Assert-True ($catalog.naive_timestamp_detail -match 'timezone offset') 'the refusal says which field is wrong and why'

# 判据 7：排序后的切片带有排序结果，且分数以文本形式离开，因此返回的值
# 就是存储的值。
Assert-Equal ($catalog.ranked_ranks -join ',') '1,2,3' 'the ranked slice carries every candidate rank'
Assert-Equal $catalog.ranked_score_of_alpha '0.900000' 'a NUMERIC score round-trips exactly'
Assert-Equal ($catalog.ranked_selected -join ',') 'True,False,True' 'selection follows the topic ordering, not the star order'

# 判据 8：唯一带授权决定的过滤。被停用的话题是未发布的配置：对 viewer 拒绝、
# 对管理员提供，且拒绝内容对它所拒绝的话题
# 只字不提。
Assert-Equal $catalog.closed_topic_status 403 'a viewer may not slice the catalog by a disabled topic'
Assert-Equal $catalog.closed_topic_leaks_name $false 'the refusal names no topic'
Assert-Equal $catalog.closed_topic_admin_status 200 'an administrator may slice by a disabled topic'
Assert-Equal $catalog.closed_topic_admin_total 3 'the administrator gets the same candidates'
Assert-Equal $catalog.missing_topic_status 404 'a topic that does not exist is a 404, not a 403'

# 判据 9：拒绝仍留在记录里。`resource_id` 才是让这行可用的关键：
# 「某人被拒绝了某物」不足以开展调查。
Assert-Equal $catalog.closed_topic_audit.count 1 'the refused slice leaves exactly one audit row'
Assert-Equal ($catalog.closed_topic_audit.resource_ids -join ',') "$($expected.closed_topic_id)" 'the refusal is filed against the topic it addressed'
Assert-Equal ($catalog.closed_topic_audit.results -join ',') 'denied' 'the refusal is recorded as a denial'

# 判据 10：趋势窗口两端都是闭的——这是平台上唯一的闭区间，因为它按日历日而不是
# 时刻切分——且首个观测上报的是缺失
# 而不是零。
#
# 窗口是与演练*请求的*那个窗口（`requested_window`）比较，绝不与
# 字面日期比较。在这里写死日期会让它成为整块断言中唯一只在脚本编写
# 当天成立的断言：演练从时钟推导自己的窗口，所以
# '2026-10-13,...' 在 2026-10-15 能通过，到下一个 UTC 午夜就会失败。
Assert-Equal $snapshots.anonymous_status 401 'an anonymous read of a trend is refused'
Assert-Equal $snapshots.closed_window_dates.Count 3 'a three-day window returns all three days'
Assert-Equal $snapshots.closed_window_dates[0] $snapshots.requested_window[0] 'the window starts on the day it was given'
Assert-Equal $snapshots.closed_window_dates[-1] $snapshots.requested_window[1] 'the window includes the day it was given as the end'
Assert-Equal ($snapshots.closed_window_resolved -join ',') ($snapshots.requested_window -join ',') 'the endpoint reports the window it served, and it is the one that was asked for'
Assert-Equal $snapshots.first_day_delta $null 'the first observation has nothing to subtract from'
Assert-Equal $snapshots.second_day_delta 30 'the second observation reports the movement'
Assert-Equal $snapshots.single_day_count 1 'a single-day window returns that day'
Assert-Equal $snapshots.single_day_date $snapshots.requested_window[1] 'a single-day window is the day that was asked for'
Assert-Equal $snapshots.empty_window_status 200 'a window with no snapshots is empty, not missing'
Assert-Equal $snapshots.empty_window_count 0 'an empty window is an empty list'

# 判据 11：默认窗口是文档记载的三十天，且每一种无法满足的窗口请求方式
# 都被拒绝而不是被收敛。
Assert-Equal $snapshots.default_window_count 3 'the default window reaches the drill snapshots'
Assert-Equal $snapshots.default_window_days $expected.default_window_days 'the default window is thirty days wide'
Assert-Equal $snapshots.inverted_window_status 422 'a window that ends before it starts is refused'
Assert-Equal $snapshots.overlong_window_status 422 'a window longer than the cap is refused rather than shortened'
Assert-Equal $snapshots.unknown_granularity_status 422 'an unknown granularity is refused'
Assert-Equal $snapshots.missing_repository_status 404 'a repository that does not exist is a 404'

# ---------------------------------------------------------------------------------------
# Audit：只追加轨迹之上的只读视图。
# ---------------------------------------------------------------------------------------

# 判据 12：读取轨迹本身就是一项特权，且拒绝会被归档。这一行是
# 从数据库读取而不是通过 API 读取的，因为被拒绝方自己能读到的拒绝
# 不是同一种控制。
Assert-Equal $audit.anonymous_status 401 'an anonymous read of the trail is refused'
Assert-Equal $audit.viewer_status 403 'a viewer may not read the audit trail'
Assert-Equal $audit.viewer_refusals 1 'the refused read leaves exactly one audit row'
Assert-Equal $audit.viewer_refusal_actor $expected.viewer_id 'the refusal names the caller it refused'
Assert-Equal $audit.viewer_refusal_resource_type 'auth' 'a role refusal is request-scoped and names no resource'

# 判据 13：轨迹按最新在前返回，且最新的一行就是刚发生的拒绝。
# `admin_total` 是更有力的那一半：三行种子数据加上管理员自己的
# 登录，说明这个端点读取的正是应用写入的台账。
Assert-Equal $audit.admin_status 200 'an administrator may read the trail'
Assert-Equal $audit.newest_first $true 'the trail is served newest first'
Assert-Equal $audit.newest_action 'auth.rbac' 'the most recent event is the refusal just recorded'
Assert-Equal $audit.newest_actor $expected.viewer_id 'the most recent event names the viewer'
Assert-Equal $audit.admin_total 4 'the administrator''s own login is part of the ledger it reads'
Assert-Equal $audit.admin_newest_action 'auth.login' 'the administrator''s newest event is its login'

# 判据 14：同一列既容纳 IPv4 也容纳 IPv6，且以文本形式离开。完全没有
# 地址的那一行是第二种情况，因为 null 和零长度 blob 很容易混淆。
Assert-Equal $audit.ipv6_rendered '2001:db8::1' 'a stored IPv6 address is rendered as text'
Assert-Equal $audit.missing_ip_rendered $null 'a request with no client address reports null'

# 判据 15：`metadata_json` 被存储且不会离开。哨兵值被植入
# 这一列本身，所以一旦这个自由格式字段在没人决定什么可以随它
# 出行的情况下被接到响应上，这里就会立刻失败。
Assert-Equal $audit.metadata_stored $true 'the free-form column really holds the sentinel'
Assert-Equal $audit.metadata_leaked $false 'neither the sentinel nor the column name appears in a response'

# 判据 16：结果过滤是封闭集合，分页上限被强制执行，说不清时区的
# 时间戳被拒绝——同样的边界修复，落在两个新端点中的第二个上。
Assert-Equal $audit.unknown_result_status 422 'a result outside the CHECK constraint is refused'
Assert-Equal $audit.page_size_cap_status 422 'a page larger than the cap is refused'
Assert-Equal $audit.naive_timestamp_status 422 'a timestamp without a timezone offset is refused here too'

# ---------------------------------------------------------------------------------------
# 演练自身的足迹，在清理之后测量。
# ---------------------------------------------------------------------------------------

# 判据 17：演练不留任何残余。`leftovers` 在清理步骤之后清点，依据的正是该步骤删除时所
# 用的同一组字面前缀，因此这是本脚本结尾打印的承诺（离开时让数据库保持被发现时的
# 原样）被落成了一件会失败的事。`audit_rows`
# 是最要紧的一项：匿名请求的拒绝会留下一行 `actor_id` 为 NULL 的记录，所以
# 基于行为者的清理永远够不到它，过去每一次运行都会往只追加的
# 台账里永久留下这么一行。
Assert-Equal $leftovers.users 0 'the drill leaves no account behind'
Assert-Equal $leftovers.repositories 0 'the drill leaves no repository behind'
Assert-Equal $leftovers.topics 0 'the drill leaves no topic behind'
Assert-Equal $leftovers.audit_rows 0 'the drill leaves no audit row behind, including the ones no actor owns'

# ---------------------------------------------------------------------------------------
# 文档结论，由同一次运行产出。
# ---------------------------------------------------------------------------------------

Assert-True $documentation.passed 'the ten documentation comparisons pass inside the smoke too'
Assert-Equal $documentation.run 10 'every comparison ran'
# `drifted` 故意是逗号连接的字符串而不是列表：PowerShell 里 `@($null).Count` 是 1，
# 所以 `@(...).Count -eq 0` 无法区分空的 JSON 数组和缺失的键。
Assert-Equal $documentation.drifted '' 'no comparison reported drift'

Write-Host '==> Leave the database as it was found'
if ($SqliteFixture) {
    & $Python (Join-Path $projectRoot 'scripts\t18_mysql_smoke.py') --cleanup-only | Out-Null
    Assert-LastCommandSucceeded 'T18 cleanup'
}
else {
    docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t18_mysql_smoke.py --cleanup-only | Out-Null
    Assert-LastCommandSucceeded 'T18 MySQL cleanup'
}

# `audited` 打印的是数量，而不是被拒话题的 `resource_ids`。这个 id 是数据库分配的
# 主键：在 Compose 数据库上是 13，在全新构建的固件上是 2，所以打印它会把一个
# 宿主相关的值放进本来与引擎无关的摘要里，恰恰招来「两份载荷不同」的
# 误读。这一行是否归档到所涉及的话题上，由下方
# 属于它的断言来验证。
Write-Host "T18 verification passed. catalog: total=$($catalog.total) order=$($catalog.full_names_ordered -join '/') providers=$($catalog.providers -join '+') unranked-nulls=$(@($rankValues | Where-Object { $null -ne $_ }).Count) disabled-topic=$($catalog.closed_topic_status)/$($catalog.closed_topic_admin_status) audited=$($catalog.closed_topic_audit.count); snapshots: window=$($snapshots.closed_window_dates.Count)d first-delta=$($snapshots.first_day_delta) second-delta=$($snapshots.second_day_delta) default=$($snapshots.default_window_days)d; audit: viewer=$($audit.viewer_status) refused=$($audit.viewer_refusals) admin-total=$($audit.admin_total) metadata-leaked=$($audit.metadata_leaked); documentation: run=$($documentation.run) passed=$($documentation.passed); leftovers: users=$($leftovers.users) repos=$($leftovers.repositories) topics=$($leftovers.topics) audit=$($leftovers.audit_rows)."

[Console]::OutputEncoding = $previousOutputEncoding
