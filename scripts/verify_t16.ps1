param(
    [string]$Python = 'python',
    [switch]$SqliteFixture
)

$ErrorActionPreference = 'Stop'
$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $projectRoot

# The smoke prints its payload as UTF-8 with `ensure_ascii=False`, because a report title is
# Chinese ('<topic> 开源技术情报周报（<date>）'). A native command's stdout is decoded using the
# console code page rather than UTF-8, so without this the title arrives as mangled bytes and
# `ConvertFrom-Json` fails part-way through the string. Both sides are pinned rather than one:
# `PYTHONIOENCODING` decides what the child *writes*, the console encoding what PowerShell *reads*.
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

# The fixture mode exists because this repository is developed on a machine whose Docker engine
# cannot start. It runs the *same* smoke module and the *same* assertion block below against a
# real SQLite file instead of MySQL, so the checks are exercised rather than merely written. The
# container mode remains the authoritative path: it is the one that also proves the migration
# that adds `backfill_jobs`, the Compose wiring, and MySQL's own constraint semantics.
#
# One hazard the container path inherits from its siblings: the smoke step mounts the host project
# root at /workspace and runs with that as its working directory, so a root `.env` is visible to
# the container. A `.env` copied verbatim from `.env.example` carries the compose-only `MYSQL_*`
# keys, which `Settings` refuses with `extra_forbidden` - and dotenv contributes keys
# independently, so the compose environment does not mask it. Remove those keys before running
# this path, or run the fixture mode.
$fixtureDir = Join-Path ([System.IO.Path]::GetTempPath()) 'osia-t16-fixture'
$fixtureDb = Join-Path $fixtureDir 't16_smoke.db'

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
    # `_env_file=None` is load-bearing: without it the fixture would read the developer's `.env`,
    # and a filled-in provider would turn this into a live, billable, non-reproducible run.
    $sitecustomize = @'
"""Repoint the T16 smoke at a SQLite file, standing in for the unavailable MySQL container."""

from __future__ import annotations

import os

from sqlalchemy import event

from open_source_intelligence.core import config as config_module

_SQLITE_URL = os.environ.get("T16_SQLITE_URL")

if _SQLITE_URL:
    _settings = config_module.Settings(
        _env_file=None,
        database_url="mysql+pymysql://app:secret@127.0.0.1:3306/open_source_intelligence",
        redis_url="redis://127.0.0.1:6379/0",
        secret_key="t16-fixture-secret-key-long-enough-for-validation",
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

    $env:T16_SQLITE_URL = "sqlite+pysqlite:///$($fixtureDb -replace '\\', '/')"
    $env:PYTHONPATH = "$fixtureDir;$projectRoot\src"

    & $Python (Join-Path $fixtureDir 'create_schema.py')
    Assert-LastCommandSucceeded 'SQLite fixture schema'

    Write-Host '==> Run the T16 backfill and weekly report smoke on the SQLite fixture'
    $smokeOutput = & $Python (Join-Path $projectRoot 'scripts\t16_mysql_smoke.py')
    Assert-LastCommandSucceeded 'T16 smoke'
}
else {
    Write-Host '==> Rebuild and start services'
    docker @composeArgs up -d --build --force-recreate --remove-orphans
    Assert-LastCommandSucceeded 'docker compose up'

    Write-Host '==> Apply migrations, prove no schema drift, and prove the new table is reversible'
    docker @composeArgs exec -T api alembic upgrade head
    Assert-LastCommandSucceeded 'alembic upgrade head'
    docker @composeArgs exec -T api alembic check
    Assert-LastCommandSucceeded 'alembic check'
    # T16 is the only stage that adds both a table and a value to an existing CHECK constraint, so
    # the round trip is asserted rather than assumed: a downgrade that forgot the `backfill` rows
    # would fail here, not in production.
    docker @composeArgs exec -T api alembic downgrade -1
    Assert-LastCommandSucceeded 'alembic downgrade -1'
    docker @composeArgs exec -T api alembic upgrade head
    Assert-LastCommandSucceeded 'alembic upgrade head (re-apply)'

    Write-Host '==> Run the T16 backfill and weekly report smoke on MySQL'
    $smokeOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t16_mysql_smoke.py
    Assert-LastCommandSucceeded 'T16 MySQL smoke'
}

# The smoke drives the real application, so its stdout also carries the application log; the
# payload is the last JSON object it prints.
$smokeJson = $smokeOutput |
    Where-Object { $_.TrimStart().StartsWith('{') } |
    Select-Object -Last 1
if (-not $smokeJson) {
    throw "T16 smoke produced no JSON payload: $smokeOutput"
}
$smoke = $smokeJson | ConvertFrom-Json

$backfill = $smoke.backfill
$report = $smoke.report
$cli = $smoke.cli
$readback = $smoke.readback

# Criterion 1: a walk stops where the source stops, not where the source's count says it should.
# The mock endpoint advertises 4210 results - roughly 1400 pages at three per page - and serves
# four. A walker that derived its last page from `total_count` would request pages that will never
# be answered; this one has to notice the absent `rel="next"`.
Assert-Equal $backfill.wide_exhausted $true 'the walk reached the end the Link header declared'
Assert-Equal ($backfill.wide_pages_requested -join ',') '1,2,3,4' 'every page the source served was requested exactly once'
Assert-Equal $backfill.wide_next_page 5 'the cursor stops one past the last page served'
Assert-Equal $backfill.wide_items 12 'the walk ingested every item the source returned'
Assert-Equal $backfill.wide_repository_rows 12 'every ingested item became a repository row'
Assert-Equal ($backfill.wide_sleeps -join ',') '1,1,1' 'the walk paced one interval between consecutive requests'
Assert-Equal $backfill.wide_paced_seconds 3 'three gaps were spent across four pages'

# Criterion 2: an interruption costs the page in flight, not the history.
Assert-Equal $backfill.failed_error_code 'COLLECTOR_FAILED' 'a source refusal is classified as a collector failure, not a bug'
Assert-Equal $backfill.failed_page 3 'the failing page is the one the smoke broke'
Assert-Equal $backfill.failed_next_page 3 'the cursor stops on the page that failed'
Assert-Equal $backfill.failed_pages_completed 2 'the two pages before the failure are committed'
Assert-Equal $backfill.failed_items_collected 6 'the committed pages account for their items'
Assert-Equal $backfill.failed_pages_remaining 2 'the job knows how much is left'
Assert-Equal $backfill.failed_resumable $true 'a failed job stays resumable'
Assert-Equal $backfill.attempts_on_failing_page 3 'the retry policy tried the failing page three times'
Assert-Equal $backfill.repository_rows_after_failure 6 'the interrupted walk only landed the pages it finished'
Assert-True $backfill.interrupted_message.Contains('HTTP 500') 'the failure message names the source answer'
Assert-Equal $backfill.failed_run_status 'failed' 'the failed walk leaves a failed run row'
Assert-Equal $backfill.failed_run_error_code 'COLLECTOR_FAILED' 'the run row carries the stable code'
Assert-Equal $backfill.runs_before_resume 1 'one attempt leaves one run row'

# Criterion 3: the resume walks the rest, and only the rest.
Assert-Equal ($backfill.resumed_pages_requested -join ',') '3,4' 'the resume requests pages 3 and 4 and nothing else'
Assert-Equal $backfill.resumed_started_page 3 'the resume starts where the walk stopped'
Assert-Equal $backfill.resumed_attempt 2 'the resume is recorded as the second attempt'
Assert-Equal $backfill.resumed_pages_completed 4 'the resumed job has completed every page'
Assert-Equal $backfill.resumed_items_collected 12 'the resumed job accounts for every item'
Assert-Equal $backfill.resumed_exhausted $true 'the resume reached the end of the source'
Assert-Equal $backfill.resumed_paced_seconds 2 'the outcome counts only this walk''s two intervals'
Assert-Equal ($backfill.sleeps_across_attempts -join ',') '1,1,1,1' 'both attempts together paced exactly four intervals'
Assert-Equal $backfill.resumed_job_status 'succeeded' 'the job row itself reads succeeded'
Assert-Equal $backfill.resumed_job_attempt 2 'the job row records two attempts'
Assert-Equal $backfill.resumed_job_pages_completed 4 'the job row records every completed page'
Assert-Equal $backfill.resumed_job_resumable $false 'a finished job is no longer resumable'
Assert-Equal $backfill.repository_rows_after_resume 12 'the resume completed the history'
Assert-Equal $backfill.runs_after_resume 2 'each attempt leaves its own run row'
Assert-Equal ($backfill.run_statuses_after_resume -join ',') 'failed,succeeded' 'the two attempts are recorded separately'

# Criterion 4: the crash window is harmless, not merely survivable. Replaying a walk cannot
# duplicate a row, because ingestion is idempotent on (source, provider_repo_id).
Assert-Equal $backfill.repository_rows_after_replay 12 'replaying the same walk adds no repository row'

# Criterion 5: the page budget is a budget, and a budget stop is distinguishable from the end of
# the source.
Assert-Equal $backfill.budget_walk_pages 2 'a walk stops on its page budget'
Assert-Equal $backfill.budget_walk_exhausted $false 'a budget stop does not claim the source was exhausted'

# Criterion 6: the budget configured on the data source row is the budget the walk spends.
Assert-Equal ($backfill.derived_limiter_budgets -join ',') $backfill.source_rate_limit_per_minute 'the walk built its limiter from the source row'
Assert-Equal $backfill.source_rate_limit_per_minute 30 'the deployed default request budget'

# Criterion 7: one cursor, one walker. Two walkers on one cursor do not merely duplicate work:
# each advances the same counter as if it were alone.
Assert-Equal $backfill.busy_held_since '2099-06-06T09:00:00+00:00' 'a live heartbeat refuses a second walker'
Assert-Equal $backfill.stale_takeover_attempt 2 'a stale heartbeat hands the cursor on'

# Criterion 8: refusals happen before anything is written, and the refusal says why.
Assert-Equal $backfill.unsupported_collector "collector 't16_not_paginated' cannot be walked page by page" 'a source that cannot page is refused by name'
Assert-True ($backfill.bound_refusals -join '|').Contains('target_pages must be between 1 and 100') 'an oversized page budget is refused'
Assert-True ($backfill.bound_refusals -join '|').Contains('per_page must be between 1 and 100') 'an oversized page is refused'
Assert-Equal $backfill.jobs_created 5 'the five walks created five jobs and the four refusals created none'
Assert-True $backfill.not_resumable_message.Contains('already completed its pages') 'resuming a finished walk is refused by name'

# Criterion 9: the ledger tells the two kinds of failure apart. `COLLECTOR_FAILED` means the
# source refused; a bug in our own writing would land on `PIPELINE_UNEXPECTED_ERROR`.
Assert-Equal ($backfill.audit_results -join ',') 'success,failed,success,success,success' 'every walk and only every walk left an audit row'

# Criterion 10: the weekly report is generated end to end, through the real pipeline.
Assert-Equal $report.daily_status 'succeeded' 'the daily pipeline succeeded'
Assert-Equal (($report.daily_stages | ForEach-Object { $_ -join ':' }) -join ',') 'collect:succeeded,snapshot:succeeded,rank:succeeded' 'collect, snapshot, and rank all ran'
Assert-Equal $report.daily_provider_counts.github 4 'the collect stage ingested the page it collected'
Assert-AtLeast $report.daily_snapshot_count 5 'the snapshot stage covered the whole catalog'
Assert-AtLeast $report.candidate_rows 2 'ranking found candidates'
Assert-Equal $report.selected_rows 2 'the topic selected exactly the candidates the Agent can afford'
Assert-AtLeast $report.readmes_cached 2 'cached content exists for the Agent to cite'
Assert-True ($report.daily_candidate_counts.PSObject.Properties.Name.Count -ge 1) 'ranking recorded a per-topic candidate count'

Assert-Equal $report.agent_status 'completed' 'the research run completed'
Assert-Equal $report.agent_section_count 6 'the draft carries all six required sections'
Assert-Equal $report.agent_budget_degraded $false 'the research walk stayed inside its budget'
Assert-Equal $report.agent_guard_triggered $false 'no untrusted content tried to give an instruction'
Assert-Equal $report.agent_prior_baseline $true 'the published history was consulted for a baseline'
Assert-Equal $report.agent_content_repos 2 'cached repository content was read'
Assert-AtLeast $report.agent_evidence 1 'the run gathered evidence'

Assert-Equal $report.report_version 1 'the first promotion produced version 1'
Assert-AtLeast $report.report_source_count 6 'the report carries its citation set'
Assert-Equal $report.verification_passed $true 'the verifier accepted the report'
Assert-Equal $report.verification_reasons.Count 0 'the verifier named no reason to hold it back'

# Criterion 11: the publishing state machine, not a direct write, carries it out.
Assert-Equal $report.decision_status 'pending_review' 'a first report is parked for a human'
Assert-Equal ($report.decision_reasons -join ',') 'FIRST_RUN' 'the policy names why'
Assert-Equal $report.review_status 'approved' 'an approval moves it to approved'
Assert-Equal $report.released_count 1 'the release sweep picked up exactly this report'
Assert-Equal $report.report_status 'published' 'the report ends up published'
Assert-Equal $report.report_published_at '2099-06-07T12:00:00+00:00' 'the release is stamped at the pinned instant'

# Criterion 12: the CLI is the operator's interface, and it agrees with the service.
Assert-Equal $cli.listed_jobs 5 'the CLI lists the jobs'
Assert-Equal $cli.listed_first_job 5 'the CLI lists newest first'
Assert-Equal $cli.status_job $backfill.first_job_id 'the CLI reports the requested job'
Assert-Equal $cli.status_status 'succeeded' 'the CLI reports the finished walk'
Assert-Equal $cli.status_resumable $false 'the CLI reports a finished job as finished'
Assert-Equal $cli.over_budget_exit_code 1 'an oversized page budget exits non-zero'
Assert-Equal $cli.over_budget_message 'target_pages must be between 1 and 100' 'the CLI explains the bound'
Assert-Equal $cli.already_done_exit_code 1 'resuming a finished walk exits non-zero'
Assert-Equal $cli.already_done_message 'backfill job 1 has already completed its pages' 'the CLI names the job it refused'

# Criterion 13: the durable rows are readable by a session that wrote nothing. The identity map
# of the writing session cannot make an uncommitted change look committed.
Assert-Equal $readback.finished_job_status 'succeeded' 'the finished job survives a re-read'
Assert-Equal $readback.finished_job_next_page 5 'its cursor survives a re-read'
Assert-Equal $readback.finished_job_items 12 'its item count survives a re-read'
Assert-Equal $readback.resumed_job_status 'succeeded' 'the resumed job survives a re-read'
Assert-Equal $readback.resumed_job_attempt 2 'its attempt count survives a re-read'
Assert-Equal $readback.resumed_job_next_page 5 'its cursor survives a re-read'
Assert-Equal $readback.resumed_job_items 12 'its item count survives a re-read'
Assert-Equal ($readback.resumed_run_statuses -join ',') 'failed,succeeded' 'both attempt ledgers survive a re-read'
Assert-Equal $readback.report_status 'published' 'the published report survives a re-read'
Assert-Equal $readback.report_version 1 'its version survives a re-read'
Assert-Equal $readback.verification_verdict 'passed' 'the passing verdict survives a re-read'
Assert-Equal $readback.approval_decision 'approve' 'the approval survives a re-read'
Assert-Equal $readback.version_row $true 'the version row survives a re-read'
Assert-AtLeast $readback.citation_rows 6 'the citation set survives a re-read'

Write-Host '==> Leave the database as it was found'
if ($SqliteFixture) {
    & $Python (Join-Path $projectRoot 'scripts\t16_mysql_smoke.py') --cleanup-only | Out-Null
    Assert-LastCommandSucceeded 'T16 cleanup'
}
else {
    docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t16_mysql_smoke.py --cleanup-only | Out-Null
    Assert-LastCommandSucceeded 'T16 MySQL cleanup'
}

Write-Host "T16 verification passed. backfill: pages=$($backfill.wide_pages_requested.Count) items=$($backfill.wide_items) resumed_from=$($backfill.resumed_started_page) idempotent=$($backfill.repository_rows_after_replay -eq $backfill.repository_rows_after_resume); report: status=$($report.report_status) sections=$($report.agent_section_count) citations=$($report.report_source_count) verified=$($report.verification_passed)."

[Console]::OutputEncoding = $previousOutputEncoding
