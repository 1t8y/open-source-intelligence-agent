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
    '-e', 'PYTHONPATH=/workspace/src',
    '--entrypoint', 'python',
    'api'
)

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

Write-Host '==> Run T7 Agent tool smoke: schema, ACL, truncation, idempotency, recording'
$smokeOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t7_mysql_smoke.py
Assert-LastCommandSucceeded 'T7 MySQL smoke'
$smoke = $smokeOutput | ConvertFrom-Json

if ($smoke.profile_ok -ne $true) {
    throw "T7 smoke profile lookup failed: $smokeOutput"
}
if ($smoke.profile_provider -ne 'github') {
    throw "T7 smoke profile provider mismatch: $smokeOutput"
}
if ($smoke.metric_points -ne 2) {
    throw "T7 smoke metric window mismatch: $smokeOutput"
}
if ($smoke.content_truncated -ne $true -or $smoke.content_cleaned -ne $true) {
    throw "T7 smoke content cleaning/truncation failed: $smokeOutput"
}
if ($smoke.content_untrusted -ne $true) {
    throw "T7 smoke content was not marked untrusted: $smokeOutput"
}
if ($smoke.prior_reports_count -ne 1) {
    throw "T7 smoke prior report query mismatch: $smokeOutput"
}
if ($smoke.unauthorized_code -ne 'TOOL_ACCESS_DENIED') {
    throw "T7 smoke ACL denial mismatch: $smokeOutput"
}
if ($smoke.unauthorized_leaked -ne $false) {
    throw "T7 smoke leaked a denied result count: $smokeOutput"
}
if ($smoke.section_first_version -ne 1 -or $smoke.section_repeat_version -ne 1) {
    throw "T7 smoke section idempotency failed: $smokeOutput"
}
if ($smoke.section_bump_version -ne 2) {
    throw "T7 smoke section versioning failed: $smokeOutput"
}
if ($smoke.section_rows -ne 2 -or $smoke.draft_rows -ne 1) {
    throw "T7 smoke produced unexpected draft rows: $smokeOutput"
}
if ($smoke.snapshot_rows_before -ne $smoke.snapshot_rows_after) {
    throw "T7 smoke read-only tool wrote domain rows: $smokeOutput"
}
if ($smoke.tool_call_rows -ne 8) {
    throw "T7 smoke tool call recording mismatch: $smokeOutput"
}
if (($smoke.denied_calls -join ',') -ne 'search_prior_reports') {
    throw "T7 smoke denial recording mismatch: $smokeOutput"
}
if (@($smoke.tool_call_statuses | Where-Object { $_ -eq 'denied' }).Count -ne 1) {
    throw "T7 smoke expected exactly one denied tool call: $smokeOutput"
}

Write-Host '==> Clean verification records'
$cleanupOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t7_mysql_smoke.py --cleanup-only
Assert-LastCommandSucceeded 'T7 cleanup'
$cleanup = $cleanupOutput | ConvertFrom-Json
if ($cleanup.cleanup_only -ne $true) {
    throw "T7 cleanup did not run: $cleanupOutput"
}

Write-Host "T7 verification passed. tool_calls=$($smoke.tool_call_rows), metric_points=$($smoke.metric_points), section_versions=1,1,2, denied=$($smoke.denied_calls -join ',')."
