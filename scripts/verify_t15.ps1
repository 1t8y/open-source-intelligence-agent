param(
    [string]$Python = 'python',
    [switch]$SqliteFixture
)

$ErrorActionPreference = 'Stop'
$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $projectRoot

# smoke 脚本以 `ensure_ascii=False` 按 UTF-8 打印其载荷，因为报告标题是中文
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

$composeArgs = @('compose', '--env-file', '.env.example', '-f', 'infra/docker-compose.yml')
$mountArgs = @(
    '-v', "${projectRoot}:/workspace",
    '-w', '/workspace',
    '-e', 'PYTHONPATH=/workspace/src:/workspace/scripts',
    '--entrypoint', 'python',
    'api'
)

# 固件模式的存在是因为本仓库在一台 Docker 引擎无法启动的机器上开发。它会用*同一个* smoke 模块和
# 下方*同一块*断言，改为针对一个真实的 SQLite 文件而非 MySQL 运行，这样这些检查是真正被执行的，而不
# 只是被写出来。容器模式仍是权威路径：只有它才能同时证明迁移、
# Compose 接线和 MySQL 自身的语义。
$fixtureDir = Join-Path ([System.IO.Path]::GetTempPath()) 'osia-t15-fixture'
$fixtureDb = Join-Path $fixtureDir 't15_smoke.db'

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
"""Repoint the T15 smoke at a SQLite file, standing in for the unavailable MySQL container."""

from __future__ import annotations

import os

from sqlalchemy import event

from open_source_intelligence.core import config as config_module

_SQLITE_URL = os.environ.get("T15_SQLITE_URL")

if _SQLITE_URL:
    _settings = config_module.Settings(
        _env_file=None,
        database_url="mysql+pymysql://app:secret@127.0.0.1:3306/open_source_intelligence",
        redis_url="redis://127.0.0.1:6379/0",
        secret_key="t15-fixture-secret-key-long-enough-for-validation",
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
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.base import Base
from open_source_intelligence.persistence.session import create_database_engine
from open_source_intelligence.core.config import get_settings

engine = create_database_engine(get_settings().database_url)
Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
print("schema ready:", len(Base.metadata.tables), "tables")
'@
    Set-Content -LiteralPath (Join-Path $fixtureDir 'create_schema.py') -Value $createSchema -Encoding utf8

    $env:T15_SQLITE_URL = "sqlite+pysqlite:///$($fixtureDb -replace '\\', '/')"
    $env:PYTHONPATH = "$fixtureDir;$projectRoot\src"

    & $Python (Join-Path $fixtureDir 'create_schema.py')
    Assert-LastCommandSucceeded 'SQLite fixture schema'

    Write-Host '==> Run the T15 observability smoke on the SQLite fixture'
    $smokeOutput = & $Python (Join-Path $projectRoot 'scripts\t15_mysql_smoke.py')
    Assert-LastCommandSucceeded 'T15 smoke'
}
else {
    Write-Host '==> Rebuild and start services'
    docker @composeArgs up -d --build --force-recreate --remove-orphans
    Assert-LastCommandSucceeded 'docker compose up'

    Write-Host '==> Apply migrations and prove no schema drift'
    docker @composeArgs exec -T api alembic upgrade head
    Assert-LastCommandSucceeded 'alembic upgrade head'
    docker @composeArgs exec -T api alembic check
    Assert-LastCommandSucceeded 'alembic check'

    Write-Host '==> Run the T15 observability smoke on MySQL'
    $smokeOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t15_mysql_smoke.py
    Assert-LastCommandSucceeded 'T15 MySQL smoke'
}

# smoke 脚本驱动的是真实应用，因此其 stdout 同时带有应用日志；
# 载荷是它打印的最后一个 JSON 对象。
$smokeJson = $smokeOutput |
    Where-Object { $_.TrimStart().StartsWith('{') } |
    Select-Object -Last 1
if (-not $smokeJson) {
    throw "T15 smoke produced no JSON payload: $smokeOutput"
}
$smoke = $smokeJson | ConvertFrom-Json

Assert-Equal $smoke.agent_provider 'offline' 'the smoke runs the offline model client'
Assert-Equal $smoke.eval_enabled $true 'the evaluation subsystem is enabled'
Assert-Equal ($smoke.metrics_roles -join ',') 'ADMIN,EDITOR' 'the roles that may read the overview'
Assert-Equal $smoke.seeded_agent_runs 3 'the smoke seeded its own run rows'

# 判据 1：聚合是正确的。每个头条数字都会与另一个从原始行重新计算它的
# 实现进行核对。
Assert-Equal $smoke.all_headline_numbers_agree $true 'every headline number matches an independent recomputation'
foreach ($metric in @(
        'pipeline_total', 'pipeline_by_status', 'pipeline_success_rate', 'pipeline_p50_ms',
        'pipeline_p95_ms', 'agent_total', 'agent_by_status', 'agent_prompt_tokens',
        'agent_completion_tokens', 'agent_total_tokens', 'agent_cost', 'agent_success_rate',
        'agent_failure_reasons', 'agent_p50_ms', 'agent_p95_ms', 'tool_total', 'tool_by_status',
        'tool_success_rate', 'report_total', 'report_by_status', 'report_manual_block_rate')) {
    Assert-Equal $smoke."matches_$metric" $true "aggregation agrees on $metric"
}

# 任务
Assert-Equal $smoke.pipeline_total 8 'pipeline rows inside the 7-day window'
Assert-Equal $smoke.pipeline_by_status.succeeded 5 'successful pipeline runs'
Assert-Equal $smoke.pipeline_by_status.failed 2 'failed pipeline runs'
Assert-Equal $smoke.pipeline_by_status.partial 1 'partially successful pipeline runs'
Assert-Equal $smoke.pipeline_success_rate '0.6250' 'pipeline success rate'
Assert-Equal $smoke.pipeline_p95_ms 80000 'pipeline P95 duration'
Assert-Equal ($smoke.pipeline_by_run_type_7d.PSObject.Properties.Name -join ',') 'agent,collect,rank,snapshot' 'pipeline run types'
Assert-Equal $smoke.pipeline_by_run_type_7d.agent.succeeded 3 'successful Agent-type pipeline runs'
Assert-Equal $smoke.pipeline_by_run_type_7d.rank.failed 1 'the failed ranking pipeline run'

# Token 与费用
Assert-Equal $smoke.agent_prompt_tokens 2500 'prompt tokens across the window'
Assert-Equal $smoke.agent_completion_tokens 550 'completion tokens across the window'
Assert-Equal $smoke.agent_total_tokens 3050 'total tokens the seeded runs spent'
Assert-Equal $smoke.agent_cost '0.020300' 'estimated cost the seeded runs recorded'

# 延迟与失败率
Assert-Equal $smoke.agent_p50_ms 40000 'Agent P50 duration'
Assert-Equal $smoke.agent_p95_ms 80000 'Agent P95 duration'
Assert-Equal $smoke.agent_success_rate '0.7500' 'Agent success rate, needs_review counted as a success'
Assert-Equal $smoke.agent_by_status.completed 2 'completed Agent runs'
Assert-Equal $smoke.agent_by_status.needs_review 1 'Agent runs parked for a human'
Assert-Equal $smoke.agent_by_status.failed 1 'failed Agent runs'
Assert-Equal $smoke.agent_failure_reasons.AGENT_TOOL_BUDGET_EXCEEDED 1 'why the failing run failed'

# 工具
Assert-Equal $smoke.tool_by_status.denied 1 'a denied tool call is recorded'
Assert-Equal $smoke.tool_by_status.failed 1 'a failed tool call is recorded'
Assert-True ($smoke.tool_total -ge 3) 'the seeded tool calls are present'

# 报告
Assert-Equal $smoke.report_total 2 'reports in the 7-day window'
Assert-Equal $smoke.report_by_status.pending_review 1 'reports parked for a human'
Assert-Equal $smoke.report_by_status.published 1 'published reports'
Assert-Equal $smoke.report_manual_block_rate '0.5000' 'manual block rate per ARCHITECTURE 12'

# 判据 3：窗口可调、有界、并且真的在过滤。
Assert-Equal $smoke.window_span_days $true 'the window spans the requested number of days'
Assert-Equal $smoke.window_is_half_open $true 'the window is half-open'
Assert-Equal $smoke.window_clamped $true 'the aggregation clamps a window wider than the bound'
Assert-Equal $smoke.windows_are_strictly_widening $true 'each wider window reaches more rows'
Assert-Equal $smoke.expected_windows_widen $true 'the independent recomputation widens the same way'
Assert-Equal $smoke.agent_total_7d 4 'Agent runs inside 7 days'
Assert-Equal $smoke.agent_total_30d 5 'Agent runs inside 30 days'
Assert-Equal $smoke.agent_total_90d 6 'Agent runs inside 90 days'
Assert-Equal $smoke.pipeline_total_7d 8 'pipeline runs inside 7 days'
Assert-Equal $smoke.pipeline_total_30d 9 'pipeline runs inside 30 days'
Assert-Equal $smoke.pipeline_total_90d 10 'pipeline runs inside 90 days'
Assert-Equal $smoke.report_total_7d 2 'reports inside 7 days'
Assert-Equal $smoke.report_total_30d 3 'reports inside 30 days'
Assert-Equal $smoke.report_total_90d 4 'reports inside 90 days'

# 判据 4：从未被触发过的指标是缺失，而不是零。
Assert-Equal $smoke.empty_window_expected_is_zero $true 'the empty window truly holds no rows'
Assert-Equal $smoke.empty_window_agent_total 0 'no Agent runs in the empty window'
Assert-Equal $null $smoke.empty_window_agent_success_rate 'an unmeasured success rate is null'
Assert-Equal $null $smoke.empty_window_agent_tokens_per_run 'unmeasured tokens per run is null'
Assert-Equal $null $smoke.empty_window_pipeline_success_rate 'an unmeasured pipeline rate is null'
Assert-Equal $null $smoke.empty_window_tool_success_rate 'an unmeasured tool rate is null'
Assert-Equal $null $smoke.empty_window_report_block_rate 'an unmeasured block rate is null'
Assert-Equal $smoke.empty_window_rates_are_null $true 'every rate in the empty window is null'
Assert-Equal $smoke.empty_window_p50_is_none $true 'an unmeasured percentile is absent'
Assert-Equal $smoke.empty_window_json_keeps_null $true 'the JSON payload keeps the distinction'
Assert-Equal $smoke.empty_window_dash_differs_from_zero $true 'absent is not rendered as zero'

# 判据 5：失败的运行可追溯到确切的工具调用。
Assert-Equal $smoke.trace_topic_name '__T15 Topic__' 'the trace names its topic'
Assert-Equal ($smoke.trace_step_types -join ',') 'plan,tool,tool,verify' 'the documented step sequence'
Assert-Equal ($smoke.trace_step_indexes -join ',') '1,2,3,4' 'steps are returned in order'
Assert-Equal $smoke.trace_step_count 4 'every step is returned'
Assert-Equal $smoke.trace_denied_tool 'get_repository_content' 'the denied tool is named'
Assert-Equal $smoke.trace_denied_error 'TOOL_NOT_ALLOWED' 'the denial carries its error code'
Assert-Equal $smoke.trace_unlinked_count 1 'a call with no step is kept, not dropped'
Assert-Equal $smoke.trace_unlinked_tool 'search_prior_reports' 'the unlinked call is named'
Assert-Equal $smoke.trace_missing_run_is_none $true 'a missing run has no trace rather than an empty one'

# 台账。
Assert-Equal $smoke.ledger_total 6 'every Agent run is listed'
Assert-Equal $smoke.ledger_newest_first $true 'the ledger is newest-first'
Assert-Equal $smoke.ledger_pages_disjoint $true 'two pages do not overlap'
Assert-Equal $smoke.ledger_page_size 5 'the requested page size is honoured'
Assert-Equal $smoke.ledger_clamped_page 1 'a page below one is clamped up'
Assert-Equal $smoke.ledger_clamped_page_size 100 'an oversized page is clamped to the bound'
Assert-Equal ($smoke.ledger_failed_only -join ',') 'failed' 'filtering by status returns that status only'
Assert-Equal $smoke.ledger_contains_failing_run $true 'the seeded failing run is in the ledger'

# 判据 2：评估结论属于评估层自己。
Assert-Equal $smoke.evaluation_available $true 'the newest sweep is surfaced'
Assert-Equal $smoke.evaluation_dataset '__t15-observability__' 'the sweep names its dataset'
Assert-Equal $smoke.evaluation_version 'v1' 'the sweep names its dataset version'
Assert-Equal $smoke.evaluation_case_count 1 'the seeded dataset has one case'
Assert-Equal ($smoke.evaluation_targets -join ',') 'baseline_single_prompt,agent_full' 'the sweep covers both variants'
Assert-Equal $smoke.evaluation_passed $true 'the verdict is read, not recomputed'
Assert-Equal $smoke.evaluation_matches_compare_latest $true 'the dashboard and compare_latest agree'
Assert-Equal $smoke.evaluation_same_in_1d_window $true 'the evaluation section is not windowed'
Assert-Equal $smoke.evaluation_target_rows.agent_full.citation_coverage '1.0000' 'the Agent backs every claim'
Assert-Equal $smoke.evaluation_target_rows.agent_full.factual_consistency '1.0000' 'the Agent agrees with the reference facts'
Assert-Equal $smoke.evaluation_target_rows.agent_full.task_completion '1.0000' 'the Agent drafts every required section'
Assert-Equal $smoke.evaluation_target_rows.agent_full.scored_case_count 1 'the Agent case was scored'
Assert-Equal $smoke.evaluation_target_rows.baseline_single_prompt.citation_coverage '0.0000' 'a single prompt cites nothing'

# JSON 端点。
Assert-Equal $smoke.api_overview_status 200 'the overview is served'
Assert-Equal ($smoke.api_overview_sections -join ',') 'agent,evaluations,pipeline,reports,tools,window' 'the six documented sections'
Assert-Equal $smoke.api_overview_window_days 7 'the requested window is echoed back'
Assert-Equal $smoke.api_all_agree_with_aggregate $true 'the endpoint forwards the aggregation unchanged'
Assert-Equal $smoke.api_agrees_pipeline_total $true 'the endpoint agrees on the task count'
Assert-Equal $smoke.api_agrees_agent_tokens $true 'the endpoint agrees on token spend'
Assert-Equal $smoke.api_agrees_agent_success_rate $true 'the endpoint agrees on the Agent failure rate'
Assert-Equal $smoke.api_agrees_tool_success_rate $true 'the endpoint agrees on the tool failure rate'
Assert-Equal $smoke.api_agrees_report_block_rate $true 'the endpoint agrees on the manual block rate'
Assert-Equal $smoke.api_agrees_agent_p95 $true 'the endpoint agrees on the P95 latency'
Assert-Equal $smoke.api_too_large_status 422 'a window above the bound is refused'
Assert-Equal $smoke.api_too_small_status 422 'a window below the bound is refused'
Assert-Equal $smoke.api_viewer_overview_status 403 'a read-only role cannot read the overview'
Assert-Equal $smoke.api_viewer_runs_status 403 'a read-only role cannot read the ledger'
Assert-Equal $smoke.api_anonymous_status 401 'an anonymous caller is unauthenticated'
Assert-Equal $smoke.api_runs_status 200 'the run ledger is served'
Assert-Equal $smoke.api_runs_include_failing $true 'the ledger carries the failing run'
Assert-Equal $smoke.api_trace_status 200 'the trajectory is served'
Assert-Equal $smoke.api_trace_step_count 4 'the trajectory carries every step'
Assert-Equal $smoke.api_trace_unlinked 1 'the trajectory carries the unlinked call'
Assert-Equal $smoke.api_trace_denied_error 'TOOL_NOT_ALLOWED' 'the trajectory carries the denial code'
Assert-Equal $smoke.api_missing_run_status 404 'a missing run is a not-found'
Assert-Equal $smoke.api_missing_run_code 'NOT_FOUND' 'a missing run carries a stable code'
Assert-Equal $smoke.api_reversed_path_status 404 'the literal path is not shadowed by the path parameter'
Assert-Equal $smoke.api_trace_redacts_secret $true 'the endpoint withholds the credential'
Assert-Equal $smoke.api_trace_keeps_mask $true 'the endpoint shows the redaction marker'

# 判据 7：两条出口路径共用同一条脱敏规则。
Assert-Equal $smoke.ui_trace_redacts_secret $true 'the page withholds the credential'
Assert-Equal $smoke.ui_trace_keeps_mask $true 'the page shows the redaction marker'

# 渲染出的仪表盘。
Assert-Equal $smoke.ui_dashboard_status 200 'an admin can render the dashboard'
Assert-Equal $smoke.ui_dashboard_is_html $true 'the dashboard is a rendered page'
Assert-Equal $smoke.ui_dashboard_names_window $true 'the page names its window'
Assert-Equal $smoke.ui_dashboard_window_switcher $true 'the page offers the window switcher'
Assert-Equal $smoke.ui_dashboard_shows_tokens $true 'the page shows the token total'
Assert-Equal $smoke.ui_dashboard_shows_success_rate $true 'the page shows the Agent success rate'
Assert-Equal $smoke.ui_dashboard_shows_block_rate $true 'the page shows the manual block rate'
Assert-Equal $smoke.ui_dashboard_shows_p50 $true 'the page shows a P50 duration'
Assert-Equal $smoke.ui_dashboard_shows_pipeline_total $true 'the page shows the task card'
Assert-Equal $smoke.ui_dashboard_shows_pipeline_total_value $true 'the page shows the task count'
Assert-Equal $smoke.ui_dashboard_shows_evaluation $true 'the page shows the evaluation section'
Assert-Equal $smoke.ui_dashboard_links_runs $true 'the page links to a run trace'
Assert-Equal $smoke.ui_month_differs_from_week $true 'a wider window renders different numbers'
Assert-Equal $smoke.ui_empty_window_uses_dash $true 'an empty window renders a dash'
Assert-Equal $smoke.ui_empty_window_more_dashes $true 'an empty window renders more dashes than a populated one'
Assert-Equal $smoke.ui_day_page_status 200 'the narrow window renders too'
Assert-Equal $smoke.ui_admin_sees_nav $true 'an admin is offered the navigation entry'
Assert-Equal $smoke.ui_editor_dashboard_status 200 'an editor can render the dashboard'
Assert-Equal $smoke.ui_editor_sees_nav $true 'an editor is offered the navigation entry'
Assert-Equal $smoke.ui_viewer_dashboard_status 403 'a viewer is refused the dashboard'
Assert-Equal $smoke.ui_viewer_dashboard_explains $true 'the refusal explains itself'
Assert-Equal $smoke.ui_viewer_reports_still_works 200 'a viewer keeps the report access they had'
Assert-Equal $smoke.ui_viewer_nav_hides_dashboard $true 'a viewer is not offered a dead link'

# 渲染出的轨迹页面。
Assert-Equal $smoke.ui_trace_status 200 'the trace page renders'
Assert-Equal $smoke.ui_trace_step_count 4 'the trace page renders every step'
Assert-Equal $smoke.ui_trace_names_denied_tool $true 'the trace page names the denied tool'
Assert-Equal $smoke.ui_trace_names_denied_error $true 'the trace page names the denial code'
Assert-Equal $smoke.ui_trace_shows_unlinked_section $true 'the trace page shows the unlinked calls'
Assert-Equal $smoke.ui_missing_run_status 404 'a missing run renders the not-found page'

Write-Host "T15 verification passed. window=7d, tasks=$($smoke.pipeline_total), tokens=$($smoke.agent_total_tokens), agent success=$($smoke.agent_success_rate), P95=$($smoke.agent_p95_ms)ms, evaluation passed=$($smoke.evaluation_passed)."

[Console]::OutputEncoding = $previousOutputEncoding
