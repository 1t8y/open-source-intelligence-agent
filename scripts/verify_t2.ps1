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

$envValues = @{}
Get-Content -LiteralPath '.env.example' | ForEach-Object {
    if ($_ -match '^([^#=]+)=(.*)$') {
        $envValues[$matches[1].Trim()] = $matches[2].Trim()
    }
}

$temporaryDatabase = 'open_source_intelligence_t2_verify'
$encodedRootPassword = [uri]::EscapeDataString($envValues['MYSQL_ROOT_PASSWORD'])
$temporaryDatabaseUrl = "mysql+pymysql://root:$encodedRootPassword@mysql:3306/${temporaryDatabase}?charset=utf8mb4"
$composeArgs = @('compose', '--env-file', '.env.example', '-f', 'infra/docker-compose.yml')

Write-Host '==> Python tests'
python -m pytest tests -q
Assert-LastCommandSucceeded 'pytest'

Write-Host '==> Ruff lint and format'
python -m ruff check src tests migrations
Assert-LastCommandSucceeded 'ruff check'
python -m ruff format --check src tests migrations
Assert-LastCommandSucceeded 'ruff format --check'

Write-Host '==> Mypy'
python -m mypy src
Assert-LastCommandSucceeded 'mypy'

Write-Host '==> Compose configuration and image rebuild'
docker @composeArgs config --quiet
Assert-LastCommandSucceeded 'docker compose config'
docker @composeArgs up -d --build --force-recreate --remove-orphans
Assert-LastCommandSucceeded 'docker compose up'

Write-Host '==> Create temporary migration database'
docker @composeArgs exec -T mysql sh -lc "mysql -uroot -p`"`$MYSQL_ROOT_PASSWORD`" -e `"DROP DATABASE IF EXISTS $temporaryDatabase; CREATE DATABASE $temporaryDatabase CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;`""
Assert-LastCommandSucceeded 'create temporary database'

try {
    Write-Host '==> Upgrade temporary database'
    docker @composeArgs run --rm --no-deps -e "DATABASE_URL=$temporaryDatabaseUrl" --entrypoint alembic api upgrade head
    Assert-LastCommandSucceeded 'alembic upgrade head'

    $tableCount = docker @composeArgs exec -T mysql sh -lc "mysql -uroot -p`"`$MYSQL_ROOT_PASSWORD`" -N -e `"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$temporaryDatabase';`""
    Assert-LastCommandSucceeded 'count tables after upgrade'
    if ([int]$tableCount.Trim() -ne 11) {
        throw "Expected 11 tables after upgrade, found $tableCount"
    }

    Write-Host '==> Check schema drift'
    docker @composeArgs run --rm --no-deps -e "DATABASE_URL=$temporaryDatabaseUrl" --entrypoint alembic api check
    Assert-LastCommandSucceeded 'alembic check'

    Write-Host '==> Downgrade temporary database'
    docker @composeArgs run --rm --no-deps -e "DATABASE_URL=$temporaryDatabaseUrl" --entrypoint alembic api downgrade base
    Assert-LastCommandSucceeded 'alembic downgrade base'

    $tableCountAfterDowngrade = docker @composeArgs exec -T mysql sh -lc "mysql -uroot -p`"`$MYSQL_ROOT_PASSWORD`" -N -e `"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$temporaryDatabase';`""
    Assert-LastCommandSucceeded 'count tables after downgrade'
    if ([int]$tableCountAfterDowngrade.Trim() -ne 1) {
        throw "Expected only alembic_version after downgrade, found $tableCountAfterDowngrade tables"
    }

    Write-Host '==> Upgrade temporary database again'
    docker @composeArgs run --rm --no-deps -e "DATABASE_URL=$temporaryDatabaseUrl" --entrypoint alembic api upgrade head
    Assert-LastCommandSucceeded 'alembic second upgrade'
}
finally {
    docker @composeArgs exec -T mysql sh -lc "mysql -uroot -p`"`$MYSQL_ROOT_PASSWORD`" -e `"DROP DATABASE IF EXISTS $temporaryDatabase;`""
}

Write-Host 'T2 verification passed. Temporary database removed.'
