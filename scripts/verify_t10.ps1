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
    '-e', 'PYTHONPATH=/workspace/src:/workspace/scripts',
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

Write-Host '==> Run T10 report composition smoke on MySQL'
$smokeOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t10_mysql_smoke.py
Assert-LastCommandSucceeded 'T10 MySQL smoke'
$smoke = $smokeOutput | ConvertFrom-Json

if ($smoke.agent_status -ne 'completed') {
    throw "T10 Agent draft did not complete: $smokeOutput"
}
if ($smoke.report_status -ne 'draft' -or $smoke.report_version -ne 2) {
    throw "T10 report status/version mismatch: $smokeOutput"
}
if ($smoke.first_created -ne $true -or $smoke.second_created -ne $false) {
    throw "T10 report idempotency mismatch: $smokeOutput"
}
if ($smoke.second_version_incremented -ne $false -or $smoke.versions_before_change -ne 1) {
    throw "T10 repeated generation created a version: $smokeOutput"
}
if ($smoke.changed_incremented -ne $true -or $smoke.changed_version -ne 2 -or $smoke.version_rows -ne 2) {
    throw "T10 version increment mismatch: $smokeOutput"
}
if ($smoke.source_count -lt 1 -or $smoke.metric_source_count -lt 1) {
    throw "T10 did not persist traceable metric sources: $smokeOutput"
}
if ($smoke.all_metric_snapshot_ids_resolved -ne $true -or $smoke.all_source_urls_https -ne $true) {
    throw "T10 citation traceability mismatch: $smokeOutput"
}
if ($smoke.invalid_citation_rejected -ne $true) {
    throw "T10 accepted a fabricated citation: $smokeOutput"
}
if ($smoke.draft_status -ne 'promoted' -or $smoke.has_full_template -ne $true) {
    throw "T10 draft promotion/template mismatch: $smokeOutput"
}

Write-Host "T10 verification passed. report=$($smoke.report_id), version=$($smoke.report_version), sources=$($smoke.source_count)."
