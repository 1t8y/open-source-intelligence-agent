param(
    [string]$Python = 'python',
    [switch]$SqliteFixture
)

$ErrorActionPreference = 'Stop'
$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $projectRoot

# The drill prints its payload as UTF-8 with `ensure_ascii=False`, because a report title is
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
# cannot always start. It runs the *same* drill module and the *same* assertion block below
# against a real SQLite file instead of MySQL, so the checks are exercised rather than merely
# written. The container mode remains the authoritative path: it is the one that also proves the
# Compose wiring and MySQL's own constraint semantics.
#
# One hazard the container path inherits from its siblings: the drill step mounts the host project
# root at /workspace and runs with that as its working directory, so a root `.env` is visible to
# the container. A `.env` copied verbatim from `.env.example` carries the compose-only `MYSQL_*`
# keys, which `Settings` refuses with `extra_forbidden` - and dotenv contributes keys
# independently, so the compose environment does not mask it. Remove those keys before running
# this path, or run the fixture mode.
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
    # `_env_file=None` is load-bearing: without it the fixture would read the developer's `.env`,
    # and a filled-in provider would turn this into a live, billable, non-reproducible run.
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

    # T17 adds no migration, so the round trip belongs to its predecessors; what it still has to
    # prove here is that the schema it runs against is the one the migrations build.
    docker @composeArgs exec -T api alembic upgrade head
    Assert-LastCommandSucceeded 'alembic upgrade head'
    docker @composeArgs exec -T api alembic check
    Assert-LastCommandSucceeded 'alembic check'

    Write-Host '==> Run the T17 security and failure drill on MySQL'
    $drillOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t17_security_drill.py
    Assert-LastCommandSucceeded 'T17 MySQL drill'
}

# The drill drives the real application, so its stdout also carries the application log; the
# payload is the last JSON object it prints.
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
# Authorization: the wrong reader and the wrong writer are refused, on both surfaces.
# ---------------------------------------------------------------------------------------

# Criterion 1: an unauthenticated caller is turned away by both transports, each in its own
# idiom - the API answers 401, the UI redirects to the login form.
Assert-Equal $authz.api_anonymous_status 401 'an anonymous API read is refused'
Assert-Equal $authz.ui_anonymous_status 303 'an anonymous UI read is redirected'
Assert-True $authz.ui_anonymous_location.StartsWith('/ui/login') 'the UI redirect keeps where the browser was heading'

# Criterion 2: the visibility filter runs inside the query, and the *total* is what proves it. A
# list that fetched every row and filtered afterwards would return the same items as this one;
# only the total tells the two apart.
Assert-Equal $authz.api_totals.admin 4 'an administrator sees every report'
Assert-Equal $authz.api_totals.editor 3 'an editor on the team scope sees three of four'
Assert-Equal $authz.api_totals.viewer 1 'a viewer sees only the internal report'
Assert-Equal ($authz.api_viewer_visible -join ',') '1' 'the viewer''s list holds exactly the internal report'
Assert-Equal ($authz.api_editor_visible -join ',') '1,2,3' 'the editor''s list holds the internal, team, and owned private reports'

# Criterion 3: a refused read carries no report metadata, and the audit row names what was
# refused. The two are the point of the split: the caller learns nothing, the operator can
# still answer "who tried to read what".
Assert-Equal $authz.api_viewer_reads_team_status 403 'a viewer may not read a team report'
Assert-Equal $authz.api_viewer_reads_orphan_status 403 'a viewer may not read an ownerless private report'
Assert-Equal $authz.api_editor_reads_orphan_status 403 'an editor may not read someone else''s private report'
Assert-Equal $authz.api_editor_reads_own_private_status 200 'an editor reads the private report it owns'
Assert-Equal $authz.orphan_title_leaked $false 'the refusal names no report'
Assert-Equal ($authz.denials.'report.read' -join ',') '2,4' 'each refused read is filed against the report it addressed'

# Criterion 4: a write is refused before it can take effect, and each guard files its own action.
Assert-Equal $authz.api_viewer_write_status 403 'a viewer may not verify a report over the API'
Assert-Equal $authz.ui_viewer_write_status 403 'a viewer may not verify a report through the UI'
Assert-Equal $authz.ui_viewer_dashboard_status 403 'a viewer may not open the operations dashboard'
Assert-Equal $authz.ui_csrf_missing_status 403 'a missing CSRF token is refused'
Assert-Equal $authz.ui_csrf_mismatch_status 403 'a mismatched CSRF token is refused'
Assert-Equal $authz.ui_editor_write_orphan_status 403 'an editor may not write to a report it cannot read'
Assert-Equal ($authz.denials.'auth.rbac' -join ',') '1,None' 'the role and page refusals are both filed'
Assert-Equal ($authz.denials.'auth.csrf' -join ',') '1' 'the CSRF refusal is filed against the report it addressed'

# ---------------------------------------------------------------------------------------
# Prompt injection: hostile README text never reaches the model as an instruction.
# ---------------------------------------------------------------------------------------

# Criterion 5: the content went through the real collector, the real snapshot and rank stages,
# and the real Agent - so the guard is being asked about content that arrived the way content
# arrives.
Assert-Equal ($injection.daily_stages -join ',') 'collect:succeeded,snapshot:succeeded,rank:succeeded' 'collect, snapshot, and rank all ran on the drill source'
Assert-Equal $injection.repositories_collected 2 'the mock source''s repositories were ingested'
Assert-Equal $injection.selected_candidates 2 'ranking selected the candidates the Agent can afford'

# Criterion 6: the instructions were stripped before the model read them, and the run says so.
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

# Criterion 7: the injection bought the attacker nothing. The tool whitelist held, no read was
# denied (nothing outside the topic was even addressed), and no credential travelled with the
# prompt.
Assert-Equal $injection.tools_invoked.Count 5 'every whitelisted tool was reachable'
Assert-True ($injection.tools_invoked -contains 'get_repository_content') 'the read that pulled the README in was the shipped tool'
Assert-Equal $injection.tool_denials 0 'the injected text caused no attempt to leave the topic scope'
Assert-Equal $injection.credentials_in_prompt.Count 0 'no deployment credential reached the model'

# ---------------------------------------------------------------------------------------
# API failure: a source that refuses is classified and recorded, never fatal to the process.
# ---------------------------------------------------------------------------------------

# Criterion 8: both a 5xx and a timeout are retried the configured three times, then recorded as
# a collector failure on the run - the distinction that keeps "the source refused" apart from
# "our own writing broke" (`PIPELINE_UNEXPECTED_ERROR`).
Assert-Equal $sourceFailure.server_error.raised_code $sourceFailure.expected_code 'a 5xx source is classified as a collector failure'
Assert-Equal $sourceFailure.server_error.run_status 'failed' 'the refused batch leaves a failed run'
Assert-Equal $sourceFailure.server_error.run_error_code 'COLLECTOR_FAILED' 'the run carries the stable code'
Assert-Equal $sourceFailure.server_error.attempts 3 'the retry policy tried the source three times'
Assert-True $sourceFailure.server_error.provider_errors 'the run records which provider failed'
Assert-Equal $sourceFailure.timeout.raised_code $sourceFailure.expected_code 'a timing-out source is classified the same way'
Assert-Equal $sourceFailure.timeout.attempts 3 'a timeout is retried on the same policy'

# Criterion 9: a model outage is a failed run with its own code, distinct from a source failure.
Assert-Equal $modelOutage.status 'failed' 'an unavailable provider fails the run'
Assert-Equal $modelOutage.error_code $modelOutage.expected_code 'the run names the provider outage'
Assert-Equal $modelOutage.run_status_persisted 'failed' 'the failure survives a re-read'
Assert-Equal $modelOutage.run_error_code_persisted 'AGENT_MODEL_UNAVAILABLE' 'the code survives a re-read'

# Criterion 10: a transport that refuses a delivery is a recorded, retryable result - not an
# exception, and not a second row.
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
# Repeated work: running the same batch twice produces one set of rows.
# ---------------------------------------------------------------------------------------

# Criterion 11: collection is idempotent on (source, provider_repo_id), so a second batch adds
# nothing - and the snapshot stage refreshes by date rather than appending.
Assert-Equal $repeat.repositories_after_first 2 'the first batch landed both repositories'
Assert-Equal $repeat.repositories_after_second 2 'the second batch added no repository row'

# Criterion 12: the release sweep and the push are both carried out by the row, not by the
# caller - a second sweep finds nothing to do and a second push sends nothing.
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

Write-Host "T17 verification passed. authorization: anonymous=$($authz.api_anonymous_status) totals=$($authz.api_totals.viewer)/$($authz.api_totals.editor)/$($authz.api_totals.admin) refused-reads=$($authz.denials.'report.read'.Count); injection: status=$($injection.agent_status) code=$($injection.agent_error_code) blocked-messages=$($injection.blocked_tool_messages); failure: source=$($sourceFailure.server_error.raised_code) model=$($modelOutage.error_code) push=$($pushFailure.first_error_code) retries=$($pushFailure.swept_accepted_attempt); repeated: repositories=$($repeat.repositories_after_second) pushes-sent=$($repeat.channel_send_count)."

[Console]::OutputEncoding = $previousOutputEncoding
