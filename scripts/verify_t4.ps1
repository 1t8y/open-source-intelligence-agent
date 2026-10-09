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

function Get-RepositoryCount {
    param([array]$ComposeArgs)

    $value = docker @ComposeArgs exec -T mysql sh -lc "mysql -uroot -p`"`$MYSQL_ROOT_PASSWORD`" -N -e `"SELECT COUNT(*) FROM open_source_intelligence.repositories;`""
    Assert-LastCommandSucceeded 'repository count'
    return [int]$value.Trim()
}

$composeArgs = @('compose', '--env-file', '.env.example', '-f', 'infra/docker-compose.yml')

Write-Host '==> Python tests'
python -m pytest tests -q
Assert-LastCommandSucceeded 'pytest'

Write-Host '==> Ruff lint and format'
python -m ruff check src tests migrations scripts/t2_mysql_smoke.py
Assert-LastCommandSucceeded 'ruff check'
python -m ruff format --check src tests migrations scripts/t2_mysql_smoke.py
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

Write-Host '==> First real collection batch'
$firstOutput = docker @composeArgs exec -T api python -m open_source_intelligence.collectors.cli --provider all --limit 2
Assert-LastCommandSucceeded 'first collection'
$firstSummary = $firstOutput | ConvertFrom-Json
if (@($firstSummary.errors.PSObject.Properties).Count -ne 0) {
    throw "First collection reported errors: $firstOutput"
}
$countAfterFirst = Get-RepositoryCount -ComposeArgs $composeArgs

Write-Host '==> Second real collection batch for idempotency'
$secondOutput = docker @composeArgs exec -T api python -m open_source_intelligence.collectors.cli --provider all --limit 2
Assert-LastCommandSucceeded 'second collection'
$secondSummary = $secondOutput | ConvertFrom-Json
if (@($secondSummary.errors.PSObject.Properties).Count -ne 0) {
    throw "Second collection reported errors: $secondOutput"
}
$countAfterSecond = Get-RepositoryCount -ComposeArgs $composeArgs

if ($countAfterFirst -ne $countAfterSecond) {
    throw "Repository count changed after repeated collection: $countAfterFirst -> $countAfterSecond"
}

Write-Host '==> Verify source and audit records'
$sourceSummary = docker @composeArgs exec -T mysql sh -lc "mysql -uroot -p`"`$MYSQL_ROOT_PASSWORD`" -N -e `"SELECT COUNT(DISTINCT source_type) FROM open_source_intelligence.data_sources;`""
Assert-LastCommandSucceeded 'source count'
if ([int]$sourceSummary.Trim() -lt 2) {
    throw "Expected GitHub and Hugging Face data sources, found $sourceSummary"
}

$auditCount = docker @composeArgs exec -T mysql sh -lc "mysql -uroot -p`"`$MYSQL_ROOT_PASSWORD`" -N -e `"SELECT COUNT(*) FROM open_source_intelligence.audit_logs WHERE action='collector.run' AND result='success';`""
Assert-LastCommandSucceeded 'collector audit count'
if ([int]$auditCount.Trim() -lt 2) {
    throw "Expected at least 2 successful collector audit events, found $auditCount"
}

Write-Host "T4 verification passed. repositories=$countAfterSecond. Services remain running."
