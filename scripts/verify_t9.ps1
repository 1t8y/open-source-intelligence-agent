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

Write-Host '==> Run T9 Prompt Guard and Token budget smoke on MySQL'
$smokeOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t9_mysql_smoke.py
Assert-LastCommandSucceeded 'T9 MySQL smoke'
$smoke = $smokeOutput | ConvertFrom-Json

if ($smoke.normal_status -ne 'completed' -or $smoke.normal_error -ne $null) {
    throw "T9 normal run mismatch: $smokeOutput"
}
if ($smoke.normal_prompt_tokens -le 0 -or $smoke.normal_completion_tokens -le 0) {
    throw "T9 normal run did not record Token usage: $smokeOutput"
}
if ($smoke.normal_run_prompt_tokens -ne $smoke.normal_prompt_tokens) {
    throw "T9 normal run Token persistence mismatch: $smokeOutput"
}
if ($smoke.degraded_error -ne 'AGENT_BUDGET_EXCEEDED' -or $smoke.degraded_flag -ne $true) {
    throw "T9 degraded run mismatch: $smokeOutput"
}
if ($smoke.degraded_sections -ne 6) {
    throw "T9 degraded run should still write six sections: $smokeOutput"
}
if ($smoke.exhausted_model_calls -ne 1 -or $smoke.exhausted_sections -ne 0) {
    throw "T9 exhausted run should stop before drafting: $smokeOutput"
}
if ($smoke.injected_error -ne 'AGENT_PROMPT_INJECTION' -or $smoke.injected_guard -ne $true) {
    throw "T9 injection guard did not mark the run: $smokeOutput"
}
if ($smoke.injected_security_risk -ne $true) {
    throw "T9 verifier did not mark the injection risk: $smokeOutput"
}
if (@($smoke.injected_unauthorized_calls).Count -ne 0) {
    throw "T9 injection caused an unauthorized tool call: $smokeOutput"
}

Write-Host "T9 verification passed. normal_tokens=$($smoke.normal_prompt_tokens + $smoke.normal_completion_tokens), degraded_sections=$($smoke.degraded_sections)."
