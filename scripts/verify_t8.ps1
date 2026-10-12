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

Write-Host '==> Run T8 research Agent smoke: full trail, recording, drafts, degraded paths'
$smokeOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t8_mysql_smoke.py
Assert-LastCommandSucceeded 'T8 MySQL smoke'
$smoke = $smokeOutput | ConvertFrom-Json

if ($smoke.status -ne 'completed') {
    throw "T8 smoke run did not complete: $smokeOutput"
}
if ($smoke.error_code -ne $null) {
    throw "T8 smoke run reported an error code: $smokeOutput"
}
if ($smoke.pipeline_status -ne 'succeeded') {
    throw "T8 smoke pipeline run status mismatch: $smokeOutput"
}
if (($smoke.step_types -join ',') -ne 'observe,plan,tool,verify,write') {
    throw "T8 smoke step types mismatch: $smokeOutput"
}
if ($smoke.step_indexes_contiguous -ne $true) {
    throw "T8 smoke step indexes are not contiguous: $smokeOutput"
}
if ($smoke.step_count -lt 15) {
    throw "T8 smoke recorded too few steps for a full trail: $smokeOutput"
}
if ($smoke.tool_call_rows -ne 13) {
    throw "T8 smoke tool call recording mismatch: $smokeOutput"
}
if ($smoke.tool_call_run_linked -ne $true) {
    throw "T8 smoke recorded a tool call against the wrong run: $smokeOutput"
}
if (($smoke.tool_call_statuses -join ',') -ne 'success') {
    throw "T8 smoke expected every tool call to succeed: $smokeOutput"
}
$expectedTools = 'get_repository_content,get_repository_profile,query_repo_metrics,save_report_section,search_prior_reports'
if (($smoke.tool_call_names -join ',') -ne $expectedTools) {
    throw "T8 smoke tool name coverage mismatch: $smokeOutput"
}
if ($smoke.section_rows -ne 6) {
    throw "T8 smoke draft section count mismatch: $smokeOutput"
}
$expectedSections = 'appendix,highlights,recommendation,risks,summary,trend'
if (($smoke.section_keys -join ',') -ne $expectedSections) {
    throw "T8 smoke draft section keys mismatch: $smokeOutput"
}
if (($smoke.section_versions -join ',') -ne '1') {
    throw "T8 smoke draft section version mismatch: $smokeOutput"
}
if ($smoke.draft_rows -ne 2) {
    throw "T8 smoke expected one draft per Agent run: $smokeOutput"
}
if ($smoke.verification_sufficient -ne $true) {
    throw "T8 smoke evidence check did not pass: $smokeOutput"
}
if ($smoke.verification_prior_baseline -ne $true) {
    throw "T8 smoke never consulted the prior report baseline: $smokeOutput"
}
if ($smoke.verification_content_repos -ne 2) {
    throw "T8 smoke did not read cached content for both candidates: $smokeOutput"
}
if ($smoke.candidate_count -ne 2) {
    throw "T8 smoke candidate selection mismatch: $smokeOutput"
}
if ($smoke.second_run_status -ne 'completed') {
    throw "T8 smoke second run was not reproducible: $smokeOutput"
}
if ($smoke.snapshot_rows_before -ne $smoke.snapshot_rows_after) {
    throw "T8 smoke read-only tools changed snapshot rows: $smokeOutput"
}
if ($smoke.content_rows_before -ne $smoke.content_rows_after) {
    throw "T8 smoke read-only tools changed repository content rows: $smokeOutput"
}
if ($smoke.repository_rows_before -ne $smoke.repository_rows_after) {
    throw "T8 smoke read-only tools changed repository rows: $smokeOutput"
}
if ($smoke.no_candidate_status -ne 'needs_review' -or $smoke.no_candidate_error -ne 'AGENT_NO_CANDIDATES') {
    throw "T8 smoke empty topic did not degrade cleanly: $smokeOutput"
}
if ($smoke.no_candidate_sections -ne 0) {
    throw "T8 smoke wrote draft sections without candidates: $smokeOutput"
}
if ($smoke.model_failure_status -ne 'failed' -or $smoke.model_failure_error -ne 'AGENT_MODEL_UNAVAILABLE') {
    throw "T8 smoke model failure did not fail cleanly: $smokeOutput"
}

Write-Host '==> Clean verification records'
$cleanupOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t8_mysql_smoke.py --cleanup-only
Assert-LastCommandSucceeded 'T8 cleanup'
$cleanup = $cleanupOutput | ConvertFrom-Json
if ($cleanup.cleanup_only -ne $true) {
    throw "T8 cleanup did not run: $cleanupOutput"
}

Write-Host "T8 verification passed. steps=$($smoke.step_count), tool_calls=$($smoke.tool_call_rows), sections=$($smoke.section_rows), candidates=$($smoke.candidate_count)."
