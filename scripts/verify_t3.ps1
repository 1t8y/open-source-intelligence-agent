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

$deadline = (Get-Date).AddSeconds(90)
$healthy = $false
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

Write-Host '==> Apply migrations and check drift'
docker @composeArgs exec -T api alembic upgrade head
Assert-LastCommandSucceeded 'alembic upgrade head'
docker @composeArgs exec -T api alembic check
Assert-LastCommandSucceeded 'alembic check'

$randomBytes = [System.Security.Cryptography.RandomNumberGenerator]::GetBytes(24)
$password = [Convert]::ToBase64String($randomBytes)
$username = 't3-verify-admin'

Write-Host '==> Bootstrap verification administrator'
docker @composeArgs exec -T -e "BOOTSTRAP_ADMIN_PASSWORD=$password" api python -m open_source_intelligence.security.bootstrap --username $username --display-name 'T3 Verify Admin'
Assert-LastCommandSucceeded 'bootstrap admin'

Write-Host '==> Login and retrieve current user'
$loginBody = @{ username = $username; password = $password } | ConvertTo-Json -Compress
$login = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/v1/auth/login' -ContentType 'application/json' -Body $loginBody
$me = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:8000/api/v1/auth/me' -Headers @{ Authorization = "Bearer $($login.access_token)" }
if ($me.username -ne $username -or $me.roles -notcontains 'ADMIN') {
    throw 'Authenticated user response did not contain the expected identity and role'
}

Write-Host '==> Verify unauthenticated access is rejected'
try {
    Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/v1/auth/me' -UseBasicParsing -ErrorAction Stop | Out-Null
    throw 'Unauthenticated request was accepted unexpectedly'
}
catch {
    if ([int]$_.Exception.Response.StatusCode -ne 401) {
        throw
    }
}

Write-Host '==> Verify audit events were persisted'
$auditCount = docker @composeArgs exec -T mysql sh -lc "mysql -uroot -p`"`$MYSQL_ROOT_PASSWORD`" -N -e `"SELECT COUNT(*) FROM open_source_intelligence.audit_logs WHERE action LIKE 'auth.%';`""
Assert-LastCommandSucceeded 'audit count'
if ([int]$auditCount.Trim() -lt 3) {
    throw "Expected at least 3 authentication audit events, found $auditCount"
}

Write-Host '==> Remove temporary verification user'
docker @composeArgs exec -T mysql sh -lc "mysql -uroot -p`"`$MYSQL_ROOT_PASSWORD`" -e `"DELETE FROM open_source_intelligence.users WHERE username='$username';`""
Assert-LastCommandSucceeded 'remove verification user'

Write-Host 'T3 verification passed. Services remain running.'
