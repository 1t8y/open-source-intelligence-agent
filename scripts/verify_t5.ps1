param(
    [switch]$Offline
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

if ($Offline) {
    # T5 的 smoke 需要的是已有的仓库行，而不是新采集的行：快照和排序只读取 T4 已经入库的
    # 数据。在没有 GitHub 出网能力的主机上，改用仍能应答的数据源补种数据，否则就复用
    # 既有数据，而不是让整个验证因为一个网络前提条件
    # 而失败。
    Write-Host '==> Offline mode: seed from the reachable source, otherwise reuse stored data'
    $seedSucceeded = $false
    $collectionOutput = docker @composeArgs exec -T api python -m open_source_intelligence.collectors.cli --provider huggingface --limit 2
    if ($LASTEXITCODE -eq 0) {
        $collectionSummary = $collectionOutput | ConvertFrom-Json
        $seedSucceeded = (@($collectionSummary.errors.PSObject.Properties).Count -eq 0) -and ([int]$collectionSummary.provider_counts.huggingface -ge 1)
    }
    if ($seedSucceeded) {
        Write-Host '    seeded from Hugging Face'
    } else {
        Write-Host '    live source unavailable; continuing with the repository data already stored by T4'
    }
} else {
    Write-Host '==> Ensure real repository data exists'
    $collectionOutput = docker @composeArgs exec -T api python -m open_source_intelligence.collectors.cli --provider all --limit 2
    Assert-LastCommandSucceeded 'collector seed'
    $collectionSummary = $collectionOutput | ConvertFrom-Json
    if (@($collectionSummary.errors.PSObject.Properties).Count -ne 0) {
        throw "Collector seed reported errors: $collectionOutput"
    }
}

Write-Host '==> Run T5 CLI smoke on MySQL'
$smokeOutput = docker @composeArgs run --rm --no-deps -v "${projectRoot}:/workspace" -w /workspace --entrypoint python api /workspace/scripts/t5_mysql_smoke.py
Assert-LastCommandSucceeded 'T5 MySQL smoke'
$smokeSummary = $smokeOutput | ConvertFrom-Json

if ($smokeSummary.repository_count -lt 1) {
    throw "T5 smoke found no repositories: $smokeOutput"
}
if ($smokeSummary.candidate_count -ne $smokeSummary.repository_count) {
    throw "T5 smoke candidate count mismatch: $smokeOutput"
}
if ($smokeSummary.topic_candidate_counts[0] -ne $smokeSummary.repository_count -or $smokeSummary.topic_candidate_counts[1] -ne $smokeSummary.repository_count) {
    throw "T5 smoke CLI ranking count mismatch: $smokeOutput"
}

Write-Host "T5 verification passed. repositories=$($smokeSummary.repository_count), candidates=$($smokeSummary.candidate_count)."
