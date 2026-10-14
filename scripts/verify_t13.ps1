param(
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $projectRoot

# The smoke prints its payload as UTF-8 with `ensure_ascii=False`, because a report title is
# Chinese ('<topic> 开源技术情报周报（<date>）'). A native command's stdout is decoded using the
# console code page rather than UTF-8, so without this the title arrives as mangled bytes and
# `ConvertFrom-Json` fails part-way through the string. Both sides are pinned rather than one:
# `PYTHONIOENCODING` decides what the child *writes*, the console encoding what PowerShell *reads*.
$previousOutputEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'

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

function Assert-Equal {
    param($Actual, $Expected, [string]$Label)

    if ($Actual -ne $Expected) {
        throw "$Label expected '$Expected' but got '$Actual'"
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

Write-Host '==> Apply migrations, check drift, and prove the T13 migration is reversible'
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

Write-Host '==> Run the T13 email/Webhook push smoke on MySQL'
$smokeOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t13_mysql_smoke.py
Assert-LastCommandSucceeded 'T13 MySQL smoke'

# The smoke drives the real application, so its stdout also carries the application log;
# the payload is the last JSON object it prints.
$smokeJson = $smokeOutput |
    Where-Object { $_.TrimStart().StartsWith('{') } |
    Select-Object -Last 1
if (-not $smokeJson) {
    throw "T13 smoke produced no JSON payload: $smokeOutput"
}
$smoke = $smokeJson | ConvertFrom-Json

Assert-Equal $smoke.push_delivery 'offline' 'default delivery transport'

# Criterion 1: only an approved or published report may be pushed.
Assert-Equal $smoke.cli_draft_push_code 'PUSH_BLOCKED' 'draft push refusal'
Assert-Equal $smoke.cli_pending_push_code 'PUSH_BLOCKED' 'scheduled push refusal'
Assert-Equal $smoke.report_b_pending_push_code 'PUSH_BLOCKED' 'pending_review push refusal'
Assert-Equal $smoke.cli_verify_verdict 'passed' 'verification before approval'
Assert-Equal $smoke.cli_approved_status 'approved' 'report reaches approved through the real review'
Assert-Equal $smoke.cli_first_push_delivered $true 'approved report is pushable'

# Criterion 2: only an allowlisted destination receives anything.
Assert-Equal $smoke.enabled_target_count 4 'enabled allowlist size'
Assert-Equal $smoke.webhook_target_count 1 'webhook channel filter'
Assert-Equal $smoke.cli_disabled_target_code 'PUSH_TARGET_NOT_ALLOWED' 'disabled target refusal'
Assert-Equal $smoke.cli_mismatched_channel_code 'PUSH_TARGET_NOT_ALLOWED' 'channel mismatch refusal'
Assert-Equal $smoke.cli_unknown_target_code 'PUSH_TARGET_NOT_ALLOWED' 'unknown target refusal'
Assert-Equal $smoke.ui_unknown_target_code $true 'unknown target refusal through the UI'

# Criterion 3: sensitive material needs an approval *and* a cleared target.
Assert-Equal $smoke.report_d_code 'PUSH_APPROVAL_REQUIRED' 'CONFIDENTIAL without approval'
Assert-Equal $smoke.report_d_rows 0 'refused delivery writes no row'
Assert-Equal $smoke.report_b_blocked_code 'PUSH_BLOCKED' 'sensitive report before approval'
Assert-Equal $smoke.report_b_approved_status 'approved' 'sensitive report approved by a human'
Assert-Equal $smoke.report_b_uncleared_code 'PUSH_TARGET_NOT_ALLOWED' 'cleared-target guard'
Assert-Equal $smoke.report_b_delivered $true 'cleared target receives the sensitive report'
Assert-Equal $smoke.report_b_rows 1 'one delivery row for the sensitive report'

# Criterion 4: delivery is idempotent and recorded.
Assert-Equal $smoke.api_first_push_status 'sent' 'first HTTP delivery'
Assert-Equal $smoke.api_first_push_delivered $true 'first HTTP delivery reached the target'
Assert-Equal $smoke.api_first_push_attempt 1 'first HTTP delivery attempt count'
Assert-Equal $smoke.api_first_push_replayed $false 'first HTTP delivery is not a replay'
Assert-Equal $smoke.api_replay_replayed $true 'repeat request replays'
Assert-Equal $smoke.api_replay_attempt 1 'replay does not send again'
Assert-Equal $smoke.api_replay_same_row $true 'replay reuses the delivery row'
Assert-Equal $smoke.cli_replay_replayed $true 'CLI repeat request replays'
Assert-Equal $smoke.cli_replay_same_row $true 'CLI replay reuses the delivery row'
Assert-Equal ($smoke.push_run_types -join ',') 'push' 'push pipeline run type'
Assert-Equal $smoke.live_build_code 'PUSH_TARGET_NOT_ALLOWED' 'unconfigured live channel is refused'
Assert-Equal $smoke.live_rows_added 0 'misconfigured live channel writes no row'
Assert-Equal $smoke.anonymous_push_status 401 'anonymous push is unauthenticated'
Assert-Equal $smoke.viewer_push_status 403 'read-only role cannot push'
Assert-Equal $smoke.viewer_retry_status 403 'read-only role cannot drive the sweep'
Assert-Equal $smoke.viewer_targets_status 403 'read-only role cannot read the allowlist'

# Criterion 5: a failed delivery is retryable on the same row.
Assert-Equal $smoke.failed_records_before 3 'failed deliveries awaiting a retry'
Assert-Equal $smoke.sweep_attempted 1 'HTTP sweep attempts the eligible row'
Assert-Equal $smoke.sweep_delivered 1 'HTTP sweep redelivers'
Assert-Equal $smoke.sweep_failed 0 'a skipped row is not a sweep failure'
Assert-Equal $smoke.sweep_replay_status 'sent' 'HTTP sweep ends the row as sent'
Assert-Equal $smoke.sweep_replay_attempt 2 'HTTP sweep advances the same row'
Assert-Equal $smoke.sweep_replay_replayed $false 'a retry actually sends'
Assert-Equal ($smoke.sweep_denied_codes -join ',') 'PUSH_TARGET_NOT_ALLOWED' 'unusable row is recorded, not fatal'
Assert-Equal $smoke.cli_sweep_attempted 1 'CLI sweep attempts the eligible row'
Assert-Equal $smoke.cli_sweep_delivered 1 'CLI sweep redelivers'
if ($smoke.rows_after_sweep.Count -ne 4) {
    throw "T13 left an unexpected delivery trail: $smokeOutput"
}
$exhausted = $smoke.rows_after_sweep | Where-Object { $_.target_id -eq $smoke.targets.cleared }
Assert-Equal $exhausted.status 'failed' 'an exhausted row is left alone'
Assert-Equal $exhausted.attempt 3 'exhausted row keeps its attempt count'
$redelivered = $smoke.rows_after_sweep | Where-Object { $_.target_id -eq $smoke.targets.webhook }
Assert-Equal $redelivered.status 'sent' 'redelivered row is sent'
Assert-Equal $redelivered.attempt 2 'redelivered row advanced in place'

# Criterion 6: a release fans out automatically.
Assert-Equal $smoke.release_scheduled_status 'scheduled' 'MANUAL report is scheduled first'
Assert-Equal $smoke.release_count 1 'the due sweep releases the report'
Assert-Equal $smoke.report_a_status 'published' 'report ends published'
Assert-Equal $smoke.report_a_rows_before_release 1 'one delivery before the release'
Assert-Equal $smoke.report_a_rows 4 'the release fans out to every enabled target'
Assert-Equal $smoke.report_a_auto_pushes 3 'auto-push records one entry per new delivery'
Assert-Equal ($smoke.report_a_channels -join ',') 'email,webhook' 'auto-push spans both channels'
Assert-Equal ($smoke.report_a_statuses -join ',') 'sent' 'every auto-push is sent'

# The UI adds the CSRF and write-role guards on top of the API.
Assert-Equal $smoke.ui_session_cookie $true 'UI session cookie'
Assert-Equal $smoke.ui_editor_login_status 303 'editor UI login'
Assert-Equal $smoke.ui_viewer_login_status 303 'viewer UI login'
Assert-Equal $smoke.ui_panel_renders_push_card $true 'push card is rendered'
Assert-Equal $smoke.ui_panel_renders_history $true 'delivery trail is rendered'
Assert-Equal $smoke.ui_panel_hint $true 'pushable-status hint is rendered'
Assert-Equal $smoke.ui_push_flash $true 'UI push flash message'
Assert-Equal $smoke.ui_push_row_sent $true 'UI push row is sent'
Assert-Equal $smoke.ui_replay_flash $true 'UI replay flash message'
Assert-Equal $smoke.ui_forged_channel_destination $true 'the channel comes from the target row'
Assert-Equal $smoke.ui_csrf_missing_status 403 'missing CSRF token'
Assert-Equal $smoke.ui_viewer_push_status 403 'read-only role cannot push from the UI'
Assert-Equal $smoke.ui_viewer_push_refused $true 'read-only push refusal message'

Write-Host "T13 verification passed. report=$($smoke.report_a), targets=$($smoke.enabled_target_count), auto-pushes=$($smoke.report_a_auto_pushes), sweeps=$($smoke.sweep_delivered + $smoke.cli_sweep_delivered)."

[Console]::OutputEncoding = $previousOutputEncoding
