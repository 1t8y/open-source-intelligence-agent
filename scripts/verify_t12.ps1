param(
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $projectRoot

# smoke 脚本以 `ensure_ascii=False` 按 UTF-8 打印其载荷，因为报告标题是中文
# （'<topic> 开源技术情报周报（<date>）'）。原生命令的 stdout 会按控制台代码页而非 UTF-8
# 解码，若不这样做，标题就会以乱码字节的形式到达，`ConvertFrom-Json` 会在字符串中间解析
# 失败。两端都要固定而不是只固定一端：`PYTHONIOENCODING` 决定子进程*写入*什么，
# 控制台编码决定 PowerShell *读取*什么。
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

Write-Host '==> Apply migrations, check drift, and prove the T12 migration is reversible'
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

Write-Host '==> Run the T12 report API and admin UI smoke on MySQL'
$smokeOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t12_mysql_smoke.py
Assert-LastCommandSucceeded 'T12 MySQL smoke'

# smoke 脚本驱动的是真实应用，因此其 stdout 同时带有应用日志；
# 载荷是它打印的最后一个 JSON 对象。
$smokeJson = $smokeOutput |
    Where-Object { $_.TrimStart().StartsWith('{') } |
    Select-Object -Last 1
if (-not $smokeJson) {
    throw "T12 smoke produced no JSON payload: $smokeOutput"
}
$smoke = $smokeJson | ConvertFrom-Json

# 判据 1：报告列表和详情视图都按调用方的数据范围过滤。
Assert-Equal $smoke.ui_anonymous_redirect $true 'anonymous UI redirect'
if ($smoke.ui_anonymous_location -notlike '/ui/login*') {
    throw "anonymous UI redirect does not target the login page: $($smoke.ui_anonymous_location)"
}
Assert-Equal $smoke.ui_viewer_login_status 303 'viewer UI login'
Assert-Equal $smoke.ui_viewer_session_cookie $true 'viewer session cookie'
Assert-Equal $smoke.ui_editor_login_status 303 'editor UI login'
Assert-Equal $smoke.ui_viewer_sees_internal $true 'viewer sees an INTERNAL report'
Assert-Equal $smoke.ui_viewer_hides_private $true 'viewer does not see a PRIVATE report'
Assert-Equal $smoke.ui_viewer_internal_detail_status 200 'viewer INTERNAL detail page'
Assert-Equal $smoke.ui_viewer_private_detail_status 403 'viewer PRIVATE detail page'
Assert-Equal $smoke.api_viewer_sees_internal $true 'API viewer list includes the INTERNAL report'
Assert-Equal $smoke.api_viewer_hides_private $true 'API viewer list excludes the PRIVATE report'
Assert-Equal $smoke.api_editor_sees_private $true 'API editor list includes the PRIVATE report'
Assert-Equal $smoke.api_admin_sees_private $true 'API admin list includes the PRIVATE report'
Assert-Equal $smoke.api_viewer_private_detail_status 403 'API viewer PRIVATE detail'
Assert-Equal $smoke.api_admin_private_detail_status 200 'API admin PRIVATE detail'
Assert-Equal $smoke.api_missing_report_status 404 'API missing report status'
Assert-Equal $smoke.api_missing_report_code 'NOT_FOUND' 'API missing report code'

# UI 在 API 之上额外加的两道防护。
Assert-Equal $smoke.ui_viewer_write_status 403 'read-only role write attempt'
Assert-Equal $smoke.ui_csrf_mismatch_status 403 'mismatched CSRF token'
Assert-Equal $smoke.ui_csrf_missing_status 403 'missing CSRF token'

# 判据 2：编辑会追加一个不可变的版本，并把报告置为待复核。
Assert-Equal $smoke.ui_verify_flash $true 'verify action flash'
Assert-Equal $smoke.verification_verdict 'passed' 'stored verdict'
Assert-Equal $smoke.edit_flash $true 'edit action flash'
Assert-Equal $smoke.edit_version 2 'version after edit'
Assert-Equal $smoke.edit_status 'draft' 'status after edit'
Assert-Equal ($smoke.edit_source_versions -join ',') '1,2' 'citation versions after edit'

# 判据 3：回滚会把历史版本恢复为一个新版本。
Assert-Equal $smoke.rollback_flash $true 'rollback action flash'
Assert-Equal $smoke.rollback_version 3 'version after rollback'
Assert-Equal $smoke.rollback_restored_from 1 'version restored from'
Assert-Equal $smoke.rollback_status 'draft' 'status after rollback'
Assert-Equal $smoke.rollback_content_restored $true 'rollback restored the old content'
Assert-Equal ($smoke.rollback_source_versions -join ',') '1,2,3' 'citation versions after rollback'

# 判据 4：MANUAL 复核闭环、审批与发布。
Assert-Equal $smoke.queue_flash $true 'publish queues a MANUAL report'
Assert-Equal $smoke.review_request_status 'pending_review' 'status after requesting review'
Assert-Equal $smoke.return_flash $true 'request_changes flash'
Assert-Equal $smoke.returned_status 'changes_requested' 'status after request_changes'
Assert-Equal $smoke.redraft_status 'draft' 'status after redrafting a returned report'
Assert-Equal $smoke.redraft_version 4 'version after redrafting'
Assert-Equal $smoke.approve_flash $true 'approve flash'
Assert-Equal $smoke.approved_status 'approved' 'status after approval'
Assert-Equal $smoke.release_flash $true 'release flash'
Assert-Equal $smoke.published_status 'published' 'status after release'

# 判据 5：已发布的报告仍可回滚，且清扫入口可从 UI 到达。
Assert-Equal $smoke.final_rollback_flash $true 'post-publish rollback flash'
Assert-Equal $smoke.final_status 'draft' 'status after post-publish rollback'
Assert-Equal $smoke.final_version 5 'version after post-publish rollback'
Assert-Equal ($smoke.final_source_versions -join ',') '1,2,3,4,5' 'citation versions at the end'
Assert-Equal ($smoke.final_versions_recorded -join ',') '1,2,3,4,5' 'version history at the end'
Assert-Equal $smoke.sweep_status 200 'publish-due sweep status'
Assert-Equal $smoke.sweep_rendered $true 'publish-due sweep rendering'

foreach ($action in @(
    'report.generate', 'report.verify', 'report.edit',
    'report.review', 'report.publish', 'report.rollback'
)) {
    if ($smoke.audit_actions -notcontains $action) {
        throw "T12 did not record the '$action' audit entry: $smokeOutput"
    }
}

if ($smoke.verification_rows -lt 3 -or $smoke.review_rows -lt 2) {
    throw "T12 did not persist verification and review evidence: $smokeOutput"
}

Write-Host "T12 verification passed. report=$($smoke.report_id), versions=$($smoke.final_version), audit=$($smoke.audit_actions -join '/')."

[Console]::OutputEncoding = $previousOutputEncoding
