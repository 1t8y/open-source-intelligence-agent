param(
    [string]$Python = 'python'
)

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
    '-e', 'PYTHONPATH=/workspace/src:/workspace/scripts',
    '--entrypoint', 'python',
    'api'
)

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

Write-Host '==> Rebuild and start services'
docker @composeArgs up -d --build --force-recreate --remove-orphans
Assert-LastCommandSucceeded 'docker compose up'

Write-Host '==> Apply migrations, check drift, and prove the T11 migration is reversible'
docker @composeArgs exec -T api alembic upgrade head
Assert-LastCommandSucceeded 'alembic upgrade head'
docker @composeArgs exec -T api alembic check
Assert-LastCommandSucceeded 'alembic check'
docker @composeArgs exec -T api alembic downgrade -1
Assert-LastCommandSucceeded 'alembic downgrade -1'
docker @composeArgs exec -T api alembic upgrade head
Assert-LastCommandSucceeded 'alembic upgrade head (re-apply)'
docker @composeArgs exec -T api alembic check
Assert-LastCommandSucceeded 'alembic check (after re-apply)'

Write-Host '==> Run T11 verifier and publishing smoke on MySQL'
$smokeOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t11_mysql_smoke.py
Assert-LastCommandSucceeded 'T11 MySQL smoke'
$smoke = $smokeOutput | ConvertFrom-Json

if ($smoke.agent_status -ne 'completed') {
    throw "T11 Agent draft did not complete: $smokeOutput"
}

# Criterion 1: a fabricated citation is caught by the Verifier.
if ($smoke.verification.failed_verdict -ne 'failed') {
    throw "T11 Verifier accepted a fabricated citation: $smokeOutput"
}
if ($smoke.verification.failed_reasons -notcontains 'CITATION_UNRESOLVED') {
    throw "T11 Verifier did not report CITATION_UNRESOLVED: $smokeOutput"
}
if ($smoke.verification.passed_verdict -ne 'passed') {
    throw "T11 Verifier rejected the restored report: $smokeOutput"
}

# Criterion 2: a clean `AUTO_WITH_DELAY` report waits out its withdrawal window.
if ($smoke.delayed.decision_status -ne 'scheduled' -or $smoke.delayed.decision_requires_review -ne $false) {
    throw "T11 clean report was not queued for delayed release: $smokeOutput"
}
if ($smoke.delayed.decision_publish_at -ne $smoke.delayed.expected_publish_at) {
    throw "T11 withdrawal window does not match publish_delay_minutes: $smokeOutput"
}
if ($smoke.delayed.window_minutes -lt 1) {
    throw "T11 withdrawal window is not configured: $smokeOutput"
}
if ($smoke.delayed.early_release_count -ne 0) {
    throw "T11 released a report before its window elapsed: $smokeOutput"
}
if ($smoke.delayed.late_released_status -ne 'published' -or $smoke.delayed.final_status -ne 'published') {
    throw "T11 did not release the report once the window elapsed: $smokeOutput"
}

# Criterion 3: `MANUAL` cannot publish without an approval record.
if ($smoke.manual.review_request_status -ne 'pending_review' -or $smoke.manual.review_request_requires_review -ne $true) {
    throw "T11 did not park the MANUAL report for review: $smokeOutput"
}
if ($smoke.manual.blocked_code -ne 'PUBLISH_POLICY_BLOCKED') {
    throw "T11 published a MANUAL report without an approval: $smokeOutput"
}
if ($smoke.manual.approved_status -ne 'approved') {
    throw "T11 review approval did not reach the approved state: $smokeOutput"
}
if ($smoke.manual.released_status -ne 'published' -or $smoke.manual.final_status -ne 'published') {
    throw "T11 did not publish the MANUAL report after approval: $smokeOutput"
}

if ($smoke.verification_rows -lt 1 -or $smoke.review_rows -lt 1) {
    throw "T11 did not persist verification and review evidence: $smokeOutput"
}

Write-Host "T11 verification passed. report=$($smoke.report_id), verified rows=$($smoke.verification_rows), reviews=$($smoke.review_rows)."
