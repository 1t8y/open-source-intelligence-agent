param()

$ErrorActionPreference = 'Stop'
$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $projectRoot

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

$composeArgs = @('compose', '--env-file', '.env.example', '-f', 'infra/docker-compose.yml')
$mountArgs = @(
    '-v', "${projectRoot}:/workspace",
    '-w', '/workspace',
    '--entrypoint', 'python',
    'api'
)
$verifyDate = '2099-02-01'
$retryDate = '2099-02-02'
$retryWorker = 'osia-t6-retry-worker'

Write-Host '==> Python tests'
python -m pytest tests -q
Assert-LastCommandSucceeded 'pytest'

Write-Host '==> Ruff lint and format'
python -m ruff check src tests migrations scripts
Assert-LastCommandSucceeded 'ruff check'
python -m ruff format --check src tests migrations scripts
Assert-LastCommandSucceeded 'ruff format --check'

Write-Host '==> Mypy'
python -m mypy src
Assert-LastCommandSucceeded 'mypy'

Write-Host '==> Rebuild and start services'
docker @composeArgs up -d --build --force-recreate --remove-orphans
Assert-LastCommandSucceeded 'docker compose up'

Write-Host '==> Apply migrations and check drift'
docker @composeArgs exec -T api alembic upgrade head
Assert-LastCommandSucceeded 'alembic upgrade head'
docker @composeArgs exec -T api alembic check
Assert-LastCommandSucceeded 'alembic check'

Write-Host '==> Ensure real repository data exists'
$collectionOutput = docker @composeArgs exec -T api python -m open_source_intelligence.collectors.cli --provider all --limit 2
Assert-LastCommandSucceeded 'collector seed'
$collectionSummary = $collectionOutput | ConvertFrom-Json
if (@($collectionSummary.errors.PSObject.Properties).Count -ne 0) {
    throw "Collector seed reported errors: $collectionOutput"
}

Write-Host '==> Run T6 MySQL smoke: run records, attempts, retry trail, constraints'
$smokeOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t6_mysql_smoke.py
Assert-LastCommandSucceeded 'T6 MySQL smoke'
$smokeSummary = $smokeOutput | ConvertFrom-Json

if ($smokeSummary.happy_status -ne 'succeeded') {
    throw "T6 smoke happy path failed: $smokeOutput"
}
if (($smokeSummary.happy_run_types -join ',') -ne 'collect,snapshot,rank') {
    throw "T6 smoke stage order mismatch: $smokeOutput"
}
if (($smokeSummary.attempt_trail -join ',') -ne '1,3') {
    throw "T6 smoke attempt trail mismatch: $smokeOutput"
}
if ($smokeSummary.verification_run_rows -ne 6) {
    throw "T6 smoke expected one run row per stage per attempt: $smokeOutput"
}
if ($smokeSummary.rerun_snapshot_rows -ne $smokeSummary.snapshot_rows_per_date) {
    throw "T6 smoke rerun duplicated snapshots: $smokeOutput"
}
if ($smokeSummary.rerun_candidate_rows -ne $smokeSummary.repository_count) {
    throw "T6 smoke candidate rows mismatch: $smokeOutput"
}
if ($smokeSummary.disabled_topic_rank_runs -ne 0) {
    throw "T6 smoke ranked a disabled topic: $smokeOutput"
}
if ($smokeSummary.failure_code -ne 'COLLECTOR_FAILED' -or $smokeSummary.failure_status -ne 'failed') {
    throw "T6 smoke failure recording mismatch: $smokeOutput"
}
if ($smokeSummary.failure_message_redacted -ne $true) {
    throw "T6 smoke stored an unredacted error message: $smokeOutput"
}
if ($smokeSummary.snapshot_runs_after_failure -ne 0) {
    throw "T6 smoke continued after a total collection failure: $smokeOutput"
}
if ($smokeSummary.topic_scope_constraint_enforced -ne $true) {
    throw "T6 smoke did not verify the topic scope constraint: $smokeOutput"
}

Write-Host '==> Dispatch the daily pipeline through the Celery broker'
$successOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t6_celery_check.py --mode success --date $verifyDate --limit 2
Assert-LastCommandSucceeded 'T6 Celery success check'
$successSummary = $successOutput | ConvertFrom-Json

if ($successSummary.outcome -ne 'succeeded' -or $successSummary.result_status -ne 'succeeded') {
    throw "T6 Celery dispatch did not succeed: $successOutput"
}
if ($successSummary.attempt -ne 1) {
    throw "T6 Celery dispatch used an unexpected attempt: $successOutput"
}
if (($successSummary.stage_types -join ',') -notlike 'collect,snapshot*') {
    throw "T6 Celery dispatch stage mismatch: $successOutput"
}

Write-Host '==> Prove retry with a dedicated worker pointed at an unreachable source'
docker @composeArgs run -d --rm --name $retryWorker `
    -e 'GITHUB_API_BASE=https://127.0.0.1:9/api' `
    -e 'PIPELINE_TASK_MAX_RETRIES=1' `
    -e 'PIPELINE_RETRY_BACKOFF_SECONDS=1' `
    --entrypoint python worker `
    -m celery -A open_source_intelligence.workers.celery_entry:app worker `
    --loglevel=WARNING --concurrency=1 -Q t6-verify
Assert-LastCommandSucceeded 'failing worker start'

try {
    $retryOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t6_celery_check.py --mode retry --date $retryDate --limit 2 --queue t6-verify --timeout 180
    Assert-LastCommandSucceeded 'T6 Celery retry check'
    $retrySummary = $retryOutput | ConvertFrom-Json

    if ($retrySummary.outcome -ne 'failed') {
        throw "T6 retry check expected a failing task: $retryOutput"
    }
    if (($retrySummary.attempts -join ',') -ne '1,2') {
        throw "T6 retry check expected two recorded attempts: $retryOutput"
    }
    if (($retrySummary.statuses | Select-Object -Unique) -ne 'failed') {
        throw "T6 retry check expected every attempt to fail: $retryOutput"
    }
    if (($retrySummary.error_codes | Select-Object -Unique) -ne 'COLLECTOR_FAILED') {
        throw "T6 retry check expected COLLECTOR_FAILED error codes: $retryOutput"
    }
}
finally {
    docker stop $retryWorker | Out-Null
}

Write-Host '==> Query pipeline run status through the CLI'
$runsOutput = docker @composeArgs exec -T api python -m open_source_intelligence.pipeline.cli --action runs --run-type collect --status failed --run-limit 10
Assert-LastCommandSucceeded 'pipeline run query'
$runsSummary = $runsOutput | ConvertFrom-Json
$failedRuns = @($runsSummary.runs)
$retryDateRuns = @($failedRuns | Where-Object { $_.metrics.business_date -eq $retryDate })
$retryAttempts = @($retryDateRuns | ForEach-Object { [int]$_.attempt })

if (@($failedRuns).Count -lt 2) {
    throw "pipeline run query returned too few failed runs: $runsOutput"
}
if (($retryAttempts -join ',') -ne '1,2') {
    throw "pipeline run query lost the retry trail: $runsOutput"
}
if (@($retryDateRuns | Where-Object { $null -eq $_.started_at -or $null -eq $_.finished_at }).Count -ne 0) {
    throw "pipeline run query returned runs without timestamps: $runsOutput"
}

Write-Host '==> Prove Beat dispatches the daily pipeline on schedule'
$baselineOutput = docker @composeArgs exec -T api python -m open_source_intelligence.pipeline.cli --action runs --run-limit 1
Assert-LastCommandSucceeded 'pipeline run baseline'
$baselineSummary = $baselineOutput | ConvertFrom-Json
$baselineRunId = 0
if (@($baselineSummary.runs).Count -gt 0) {
    $baselineRunId = [int]$baselineSummary.runs[0].run_id
}

$scheduledBeat = 'osia-t6-scheduled-beat'
$target = (Get-Date).AddMinutes(2)
docker @composeArgs run -d --rm --name $scheduledBeat `
    -e "PIPELINE_SCHEDULE_HOUR=$($target.Hour)" `
    -e "PIPELINE_SCHEDULE_MINUTE=$($target.Minute)" `
    --entrypoint python beat `
    -m celery -A open_source_intelligence.workers.celery_entry:app beat `
    --loglevel=WARNING -s /tmp/celerybeat-t6
Assert-LastCommandSucceeded 'scheduled beat start'

$scheduledRunId = 0
try {
    $deadline = (Get-Date).AddSeconds(240)
    while ((Get-Date) -lt $deadline -and $scheduledRunId -le $baselineRunId) {
        Start-Sleep -Seconds 5
        $pollOutput = docker @composeArgs exec -T api python -m open_source_intelligence.pipeline.cli --action runs --run-limit 1
        if ($LASTEXITCODE -ne 0) {
            continue
        }
        $pollSummary = $pollOutput | ConvertFrom-Json
        if (@($pollSummary.runs).Count -gt 0) {
            $newestRunId = [int]$pollSummary.runs[0].run_id
            if ($newestRunId -gt $baselineRunId) {
                $scheduledRunId = $newestRunId
            }
        }
    }
}
finally {
    docker stop $scheduledBeat | Out-Null
}

if ($scheduledRunId -le $baselineRunId) {
    throw "Beat did not dispatch the daily pipeline for the scheduled minute (baseline=$baselineRunId)"
}

Write-Host "==> Clean verification records"
$cleanupOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t6_mysql_smoke.py --cleanup-only --extra-cleanup-dates $retryDate
Assert-LastCommandSucceeded 'T6 cleanup'
$cleanupSummary = $cleanupOutput | ConvertFrom-Json
if ($cleanupSummary.cleanup_only -ne $true) {
    throw "T6 cleanup did not run: $cleanupOutput"
}

Write-Host "T6 verification passed. run_rows=$($smokeSummary.verification_run_rows), attempts=$($smokeSummary.attempt_trail -join ','), retry_attempts=$($retrySummary.attempts -join ','), candidates=$($smokeSummary.topic_candidate_counts.PSObject.Properties.Name.Count)."
