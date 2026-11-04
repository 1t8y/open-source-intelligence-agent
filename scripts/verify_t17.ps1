param(
    [string]$Python = 'python',
    [switch]$SqliteFixture
)

$ErrorActionPreference = 'Stop'
$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $projectRoot

# 演练以 `ensure_ascii=False` 按 UTF-8 打印其载荷，因为报告标题是中文
# （'<topic> 开源技术情报周报（<date>）'）。原生命令的 stdout 会按控制台代码页而非 UTF-8
# 解码，若不这样做，标题就会以乱码字节的形式到达，`ConvertFrom-Json` 会在字符串中间解析
# 失败。两端都要固定而不是只固定一端：`PYTHONIOENCODING` 决定子进程*写入*什么，
# 控制台编码决定 PowerShell *读取*什么。
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

function Assert-AtLeast {
    param($Actual, $Floor, [string]$Label)

    if ($Actual -lt $Floor) {
        throw "$Label expected at least '$Floor' but got '$Actual'"
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

# 固件模式的存在是因为本仓库在一台 Docker 引擎并非总能启动的机器上开发。它会用*同一个*演练模块
# 和下方*同一块*断言，改为针对一个真实的 SQLite 文件而非 MySQL 运行，这样这些检查是真正被执行的，
# 而不只是被写出来。容器模式仍是权威路径：只有它才能同时证明 Compose 接线和 MySQL 自身的
# 约束语义。
#
# 容器路径从它的兄弟脚本继承了一个隐患：演练步骤把宿主项目根目录挂载到 /workspace 并以它为
# 工作目录运行，因此根目录的 `.env` 对容器可见。一份从 `.env.example` 原样复制的 `.env` 带有仅供
# compose 使用的 `MYSQL_*` 键，`Settings` 会以 `extra_forbidden` 拒绝它们——而且 dotenv 是独立
# 注入键的，compose 环境无法掩盖它。运行此路径前请移除那些键，或者改跑固件模式。
$fixtureDir = Join-Path ([System.IO.Path]::GetTempPath()) 'osia-t17-fixture'
$fixtureDb = Join-Path $fixtureDir 't17_drill.db'

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

if ($SqliteFixture) {
    Write-Host "==> Prepare the SQLite fixture at $fixtureDir"
    New-Item -ItemType Directory -Force -Path $fixtureDir | Out-Null
    # `_env_file=None` 是关键所在：没有它，固件会读取开发者的 `.env`，
    # 而一旦其中填写了某个供应商配置，就会把这次运行变成一次真实计费且不可复现的运行。
    $sitecustomize = @'
"""Repoint the T17 drill at a SQLite file, standing in for an unavailable MySQL container."""

from __future__ import annotations

import os

from sqlalchemy import event

from open_source_intelligence.core import config as config_module

_SQLITE_URL = os.environ.get("T17_SQLITE_URL")

if _SQLITE_URL:
    _settings = config_module.Settings(
        _env_file=None,
        database_url="mysql+pymysql://app:secret@127.0.0.1:3306/open_source_intelligence",
        redis_url="redis://127.0.0.1:6379/0",
        secret_key="t17-fixture-secret-key-long-enough-for-validation",
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

    $env:T17_SQLITE_URL = "sqlite+pysqlite:///$($fixtureDb -replace '\\', '/')"
    $env:PYTHONPATH = "$fixtureDir;$projectRoot\src;$projectRoot\scripts"

    & $Python (Join-Path $fixtureDir 'create_schema.py')
    Assert-LastCommandSucceeded 'SQLite fixture schema'

    Write-Host '==> Run the T17 security and failure drill on the SQLite fixture'
    $drillOutput = & $Python (Join-Path $projectRoot 'scripts\t17_security_drill.py')
    Assert-LastCommandSucceeded 'T17 drill'
}
else {
    Write-Host '==> Rebuild and start services'
    docker @composeArgs up -d --build --force-recreate --remove-orphans
    Assert-LastCommandSucceeded 'docker compose up'

    # T17 没有新增迁移，因此往返（降级再升级）属于它的前序阶段；它在这里仍需
    # 证明的是：它所运行的 schema 就是迁移构建出来的那一个。
    docker @composeArgs exec -T api alembic upgrade head
    Assert-LastCommandSucceeded 'alembic upgrade head'
    docker @composeArgs exec -T api alembic check
    Assert-LastCommandSucceeded 'alembic check'

    Write-Host '==> Run the T17 security and failure drill on MySQL'
    $drillOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t17_security_drill.py
    Assert-LastCommandSucceeded 'T17 MySQL drill'
}

# 演练驱动的是真实应用，因此其 stdout 同时带有应用日志；
# 载荷是它打印的最后一个 JSON 对象。
$drillJson = $drillOutput |
    Where-Object { $_.TrimStart().StartsWith('{') } |
    Select-Object -Last 1
if (-not $drillJson) {
    throw "T17 drill produced no JSON payload: $drillOutput"
}
$drill = $drillJson | ConvertFrom-Json

$authz = $drill.authorization
$injection = $drill.injection
$sourceFailure = $drill.source_failure
$modelOutage = $drill.model_outage
$pushFailure = $drill.push_failure
$repeat = $drill.repeated_work

# ---------------------------------------------------------------------------------------
# Authorization：错误的读者与错误的写入者都被拒绝，且两个界面上一视同仁。
# ---------------------------------------------------------------------------------------

# 判据 1：未认证的调用者在两种传输方式下都被拒之门外，且各有各的
# 惯用方式——API 回 401，UI 重定向到登录表单。
Assert-Equal $authz.api_anonymous_status 401 'an anonymous API read is refused'
Assert-Equal $authz.ui_anonymous_status 303 'an anonymous UI read is redirected'
Assert-True $authz.ui_anonymous_location.StartsWith('/ui/login') 'the UI redirect keeps where the browser was heading'

# 判据 2：可见性过滤发生在查询内部，而*total*（总数）才是这一点的证明。一个先取出全部行
# 再事后过滤的列表会返回与它相同的条目；只有总数能区分两者。
#
# 这里的每个期望值都来自载荷而不是字面量，因为报告 id 不是演练能选择的：本脚本从不删除
# 数据库卷，而 MySQL 在删除行后不会重置 AUTO_INCREMENT，所以在一个用过的数据库上，演练的
# 四份报告并不从 id 1 开始——演练跑完一轮后表又空了，下一份报告仍取下一个 id。以前放在这里
# 的字面量（`'1'`、`'1,2,3'`、`'2,4'`、`'1,None'`）在全新的卷上通过、在用过的卷上失败，
# 两边的代码却完全相同。演练在写入之前先测量各账号能读到什么
# （`measure_visibility_baseline`），并上报自己创建的 id，因此这里断言的是「API 返回的恰好
# 是演练的四份报告，且归档在正确的可见性下」，而不是「运维者的卷是干净的」。
Assert-Equal $authz.api_totals.admin $authz.expected_totals.admin 'an administrator sees every report'
Assert-Equal $authz.api_totals.editor $authz.expected_totals.editor 'an editor on the team scope sees the internal, team, and owned private reports'
Assert-Equal $authz.api_totals.viewer $authz.expected_totals.viewer 'a viewer sees only the internal report'
Assert-Equal ($authz.api_admin_visible -join ',') ($authz.expected_visible.admin -join ',') 'the administrator''s list holds every report'
Assert-Equal ($authz.api_viewer_visible -join ',') ($authz.expected_visible.viewer -join ',') 'the viewer''s list holds exactly the internal report'
Assert-Equal ($authz.api_editor_visible -join ',') ($authz.expected_visible.editor -join ',') 'the editor''s list holds the internal, team, and owned private reports'

# 判据 3：被拒绝的读取不携带任何报告元数据，而审计行会写明被拒绝的是什么。
# 这种一分为二正是要点：调用方什么都得不到，运维者仍能
# 回答「谁试图读什么」。
Assert-Equal $authz.api_viewer_reads_team_status 403 'a viewer may not read a team report'
Assert-Equal $authz.api_viewer_reads_orphan_status 403 'a viewer may not read an ownerless private report'
Assert-Equal $authz.api_editor_reads_orphan_status 403 'an editor may not read someone else''s private report'
Assert-Equal $authz.api_editor_reads_own_private_status 200 'an editor reads the private report it owns'
Assert-Equal $authz.orphan_title_leaked $false 'the refusal names no report'
Assert-Equal ($authz.denials.'report.read' -join ',') ($authz.expected_denials.'report.read' -join ',') 'each refused read is filed against the report it addressed'

# 判据 4：写入在生效之前就被拒绝，且每道防护各自记录自己的动作。
Assert-Equal $authz.api_viewer_write_status 403 'a viewer may not verify a report over the API'
Assert-Equal $authz.ui_viewer_write_status 403 'a viewer may not verify a report through the UI'
Assert-Equal $authz.ui_viewer_dashboard_status 403 'a viewer may not open the operations dashboard'
Assert-Equal $authz.ui_csrf_missing_status 403 'a missing CSRF token is refused'
Assert-Equal $authz.ui_csrf_mismatch_status 403 'a mismatched CSRF token is refused'
Assert-Equal $authz.ui_editor_write_orphan_status 403 'an editor may not write to a report it cannot read'
Assert-Equal ($authz.denials.'auth.rbac' -join ',') ($authz.expected_denials.'auth.rbac' -join ',') 'the role and page refusals are both filed'
Assert-Equal ($authz.denials.'auth.csrf' -join ',') ($authz.expected_denials.'auth.csrf' -join ',') 'the CSRF refusal is filed against the report it addressed'

# ---------------------------------------------------------------------------------------
# Prompt injection：恶意的 README 文本永远不会以指令的形式到达模型。
# ---------------------------------------------------------------------------------------

# 判据 5：内容经过了真实的采集器、真实的快照与排序阶段、以及真实的
# Agent——因此被问及防护的内容，正是以内容本来的到达方式
# 到达的内容。
Assert-Equal ($injection.daily_stages -join ',') 'collect:succeeded,snapshot:succeeded,rank:succeeded' 'collect, snapshot, and rank all ran on the drill source'
Assert-Equal $injection.repositories_collected 2 'the mock source''s repositories were ingested'
Assert-Equal $injection.selected_candidates 2 'ranking selected the candidates the Agent can afford'

# 判据 6：指令在模型读取之前就被剥离，且运行记录说明了这一点。
Assert-Equal $injection.agent_status 'needs_review' 'an injection parks the run for a human'
Assert-Equal $injection.agent_error_code 'AGENT_PROMPT_INJECTION' 'the run names the injection'
Assert-Equal $injection.guard_triggered $true 'the guard reports it fired'
Assert-Equal $injection.verification_security_risk $true 'the verifier carries the risk flag forward'
Assert-Equal $injection.run_status_persisted 'needs_review' 'the parked status survives a re-read'
Assert-Equal $injection.run_error_code_persisted 'AGENT_PROMPT_INJECTION' 'the error code survives a re-read'
Assert-AtLeast $injection.blocked_tool_messages 1 'the model was handed the guarded payload'
Assert-Equal $injection.raw_injection_reached_model $false 'the English instruction never reached the model'
Assert-Equal $injection.chinese_injection_reached_model $false 'the Chinese instruction never reached the model'
Assert-Equal $injection.normal_text_survived $true 'the guard removed instructions and nothing else'

# 判据 7：注入没有给攻击者换来任何东西。工具白名单守住了，没有读取被拒绝（话题之外的内容
# 甚至未被触及），也没有任何凭据随
# 提示词一同出行。
Assert-Equal $injection.tools_invoked.Count 5 'every whitelisted tool was reachable'
Assert-True ($injection.tools_invoked -contains 'get_repository_content') 'the read that pulled the README in was the shipped tool'
Assert-Equal $injection.tool_denials 0 'the injected text caused no attempt to leave the topic scope'
Assert-Equal $injection.credentials_in_prompt.Count 0 'no deployment credential reached the model'

# ---------------------------------------------------------------------------------------
# API failure：拒绝应答的源头会被分类并记录，绝不会让进程崩溃。
# ---------------------------------------------------------------------------------------

# 判据 8：5xx 与超时都按配置重试三次，然后作为采集失败记录在运行上——正是这个区分把
# 「源头拒绝」与「我们自己的写入坏了」（`PIPELINE_UNEXPECTED_ERROR`）区分开来。
Assert-Equal $sourceFailure.server_error.raised_code $sourceFailure.expected_code 'a 5xx source is classified as a collector failure'
Assert-Equal $sourceFailure.server_error.run_status 'failed' 'the refused batch leaves a failed run'
Assert-Equal $sourceFailure.server_error.run_error_code 'COLLECTOR_FAILED' 'the run carries the stable code'
Assert-Equal $sourceFailure.server_error.attempts 3 'the retry policy tried the source three times'
Assert-True $sourceFailure.server_error.provider_errors 'the run records which provider failed'
Assert-Equal $sourceFailure.timeout.raised_code $sourceFailure.expected_code 'a timing-out source is classified the same way'
Assert-Equal $sourceFailure.timeout.attempts 3 'a timeout is retried on the same policy'

# 判据 9：模型不可用是一次带独立错误码的失败运行，与源头失败不同。
Assert-Equal $modelOutage.status 'failed' 'an unavailable provider fails the run'
Assert-Equal $modelOutage.error_code $modelOutage.expected_code 'the run names the provider outage'
Assert-Equal $modelOutage.run_status_persisted 'failed' 'the failure survives a re-read'
Assert-Equal $modelOutage.run_error_code_persisted 'AGENT_MODEL_UNAVAILABLE' 'the code survives a re-read'

# 判据 10：拒绝投递的传输通道是一个被记录、可重试的结果——不是
# 异常，也不是第二行。
Assert-Equal $pushFailure.first_status 'failed' 'a refused delivery is recorded, not raised'
Assert-Equal $pushFailure.first_error_code 'PUSH_DELIVERY_FAILED' 'the row carries the stable code'
Assert-Equal $pushFailure.first_delivered $false 'a refused delivery is not a delivery'
Assert-Equal $pushFailure.refusing_attempts 2 'the first attempt and one retry both hit the refusing transport'
Assert-Equal ($pushFailure.swept_refused_status -join ',') 'failed' 'the retry sweep records the second refusal'
Assert-Equal ($pushFailure.swept_refused_attempt -join ',') '2' 'the retry advances the attempt count'
Assert-Equal ($pushFailure.swept_accepted_status -join ',') 'sent' 'a retry against a working transport succeeds'
Assert-Equal ($pushFailure.swept_accepted_attempt -join ',') '3' 'the successful retry is recorded as the third attempt'
Assert-Equal $pushFailure.push_rows 1 'one (report, version, target) keeps exactly one row'
Assert-Equal $pushFailure.final_row_status 'sent' 'the row ends up delivered'
Assert-Equal $pushFailure.replayed $true 're-requesting a delivered push replays the stored result'

# ---------------------------------------------------------------------------------------
# Repeated work：同一批次跑两遍只产生一套行。
# ---------------------------------------------------------------------------------------

# 判据 11：采集在 (source, provider_repo_id) 上是幂等的，因此第二个批次不会
# 新增任何内容——且快照阶段按日期刷新而不是追加。
Assert-Equal $repeat.repositories_after_first 2 'the first batch landed both repositories'
Assert-Equal $repeat.repositories_after_second 2 'the second batch added no repository row'

# 判据 12：发布清扫与推送都由行本身完成，而不是由调用方完成——第二次
# 清扫无事可做，第二次推送也不发送任何内容。
Assert-Equal $repeat.first_publish_sweep 0 'the first sweep finds no scheduled report'
Assert-Equal $repeat.second_publish_sweep 0 'the second sweep finds nothing either'
Assert-Equal $repeat.first_push_replayed $false 'the first push to a fresh target really delivers'
Assert-Equal $repeat.second_push_replayed $true 'the second push replays the stored result'
Assert-Equal $repeat.channel_send_count 1 'two push requests reach the transport once'

Write-Host '==> Leave the database as it was found'
if ($SqliteFixture) {
    & $Python (Join-Path $projectRoot 'scripts\t17_security_drill.py') --cleanup-only | Out-Null
    Assert-LastCommandSucceeded 'T17 cleanup'
}
else {
    docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t17_security_drill.py --cleanup-only | Out-Null
    Assert-LastCommandSucceeded 'T17 MySQL cleanup'
}

# 摘要行是两个引擎相互比较的依据，因此它上报的是本次演练为每个角色的列表新增的*增量*，
# 而不是绝对总数：总数包含了数据库里已有的内容（`baseline_totals`），而本脚本从不删除
# 数据卷。"+1/+3/+4" 在全新的固件上和用过的 Compose 数据库上说的是同一件事。
Write-Host "T17 verification passed. authorization: anonymous=$($authz.api_anonymous_status) seeded-totals=+$($authz.api_totals.viewer - $authz.baseline_totals.viewer)/+$($authz.api_totals.editor - $authz.baseline_totals.editor)/+$($authz.api_totals.admin - $authz.baseline_totals.admin) refused-reads=$($authz.denials.'report.read'.Count); injection: status=$($injection.agent_status) code=$($injection.agent_error_code) blocked-messages=$($injection.blocked_tool_messages); failure: source=$($sourceFailure.server_error.raised_code) model=$($modelOutage.error_code) push=$($pushFailure.first_error_code) retries=$($pushFailure.swept_accepted_attempt); repeated: repositories=$($repeat.repositories_after_second) pushes-sent=$($repeat.channel_send_count)."

[Console]::OutputEncoding = $previousOutputEncoding
