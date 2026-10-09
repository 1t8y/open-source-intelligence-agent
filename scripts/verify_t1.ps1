param(
    [switch]$SkipCompose
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

Write-Host '==> Python tests'
python -m pytest tests -q
Assert-LastCommandSucceeded 'pytest'

Write-Host '==> Ruff lint'
python -m ruff check src tests
Assert-LastCommandSucceeded 'ruff check'

Write-Host '==> Ruff format'
python -m ruff format --check src tests
Assert-LastCommandSucceeded 'ruff format --check'

Write-Host '==> Mypy'
python -m mypy src
Assert-LastCommandSucceeded 'mypy'

if ($SkipCompose) {
    Write-Host 'T1 verification passed without Compose smoke.'
    exit 0
}

$composeArgs = @('compose', '--env-file', '.env.example', '-f', 'infra/docker-compose.yml')

Write-Host '==> Compose configuration'
docker @composeArgs config --quiet
Assert-LastCommandSucceeded 'docker compose config'

Write-Host '==> Start Compose stack'
docker @composeArgs up -d --build --force-recreate --remove-orphans
Assert-LastCommandSucceeded 'docker compose up'

Write-Host '==> Wait for API health'
$healthy = $false
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -eq 200) {
            $healthy = $true
            break
        }
    }
    catch {
        Start-Sleep -Seconds 2
    }
}

if (-not $healthy) {
    throw 'API health check did not become ready within 90 seconds'
}

Write-Host '==> Verify Beat is running'
$beatContainer = docker @composeArgs ps --status running --quiet beat
Assert-LastCommandSucceeded 'docker compose ps beat'
if ([string]::IsNullOrWhiteSpace($beatContainer)) {
    throw 'Beat container is not running'
}

Write-Host '==> Verify Worker consumes a task'
$taskId = docker @composeArgs exec -T api python -m celery -A open_source_intelligence.workers.celery_entry:app call open_source_intelligence.workers.tasks.healthcheck
Assert-LastCommandSucceeded 'Celery task dispatch'
$taskId = $taskId.Trim()

$taskReceived = $false
$taskSucceeded = $false
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    $workerLogs = docker @composeArgs logs --no-color worker --tail 200
    Assert-LastCommandSucceeded 'Worker log read'
    $taskLines = $workerLogs | Where-Object { $_ -match [regex]::Escape($taskId) }
    $taskReceived = [bool]($taskLines | Where-Object { $_ -match 'received' })
    $taskSucceeded = [bool]($taskLines | Where-Object { $_ -match 'succeeded' })
    if ($taskReceived -and $taskSucceeded) {
        break
    }
    Start-Sleep -Seconds 1
}

if (-not $taskReceived) {
    throw "Worker did not receive task $taskId"
}
if (-not $taskSucceeded) {
    throw "Worker did not report task $taskId as succeeded"
}

Write-Host 'T1 verification passed. Compose stack remains running for preview.'
