param(
    [string]$Python = 'python',
    [switch]$SqliteFixture
)

$ErrorActionPreference = 'Stop'
$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $projectRoot

# The smoke prints its payload as UTF-8 with `ensure_ascii=False`, because the drill writes and
# reads rows whose text is Chinese. A native command's stdout is decoded using the console code
# page rather than UTF-8, so without this the payload arrives as mangled bytes and
# `ConvertFrom-Json` fails part-way through a string. Both sides are pinned rather than one:
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

function Assert-True {
    param($Condition, [string]$Label)

    if (-not $Condition) {
        throw "$Label expected a true condition"
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

# The fixture mode exists because this repository is developed on a machine whose Docker engine
# cannot always start. It runs the *same* smoke module and the *same* assertion block below
# against a real SQLite file instead of MySQL, so the checks are exercised rather than merely
# written. The container mode remains the authoritative path: it is the one that also proves the
# Compose wiring, MySQL's own constraint semantics, and - as T18 discovered - that the
# documentation checker can still see the routes under a different FastAPI version.
$fixtureDir = Join-Path ([System.IO.Path]::GetTempPath()) 'osia-t18-fixture'
$fixtureDb = Join-Path $fixtureDir 't18_smoke.db'

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

# ---------------------------------------------------------------------------------------
# Documentation consistency: the ten comparisons, and proof that they can fail.
# ---------------------------------------------------------------------------------------

# The host interpreter has no project dependencies installed, so the checker is pointed at the
# source tree the same way the fixture mode points the smoke at it.
$env:PYTHONPATH = "$projectRoot\src;$projectRoot\scripts"

$driftScript = Join-Path $projectRoot 'scripts\check_docs_drift.py'
$mutatedPaths = @(
    (Join-Path $projectRoot 'docs\api-contract.md'),
    (Join-Path $projectRoot 'docs\data-model.md'),
    (Join-Path $projectRoot 'ARCHITECTURE.md')
)

Write-Host '==> Documentation consistency'
$checkOutput = & $Python $driftScript 2>&1
Assert-LastCommandSucceeded 'documentation consistency check'
$checkJson = $checkOutput | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1
if (-not $checkJson) {
    throw "the documentation checker produced no JSON payload: $checkOutput"
}
$drift = $checkJson | ConvertFrom-Json
Assert-Equal $drift.failures 0 'the documentation set matches the code'
Assert-True $drift.passed 'every documentation check passes'
foreach ($name in $drift.checks.PSObject.Properties.Name) {
    Assert-True $drift.checks.($name).ok "documentation check '$name' reports no drift"
}
Assert-Equal $drift.checks.endpoints.notes[0] 'registered paths: 41' 'the route walk finds every registered path'
Assert-Equal $drift.checks.'architecture-summary'.notes[0] '§7 lists 29 of 29 tables' 'the architecture summary lists every table'
Assert-Equal $drift.checks.'status-lines'.notes[0] 'last completed task: T18' 'the status lines are compared to the task list, not to a hand-kept number'
Assert-Equal $drift.checks.'evaluation-targets'.notes[0] 'variants: 5; promotion: agent_full' 'the evaluation variants match TARGET_ORDER'

# The check that matters most here is not that the checker passes but that it *can* fail. A
# comparison that always answered "consistent" would have been no better than the reading it
# replaced, and the four phantom resource groups would still be in the contract today. So three
# documents are temporarily made wrong - one per direction of the API comparison, one for the
# data model, and the task list itself - and the checker is required to report all three before
# the originals are put back.
Write-Host '==> Prove the checker can fail'
# The originals are held in memory, not copied to a `.t18backup` file. A backup file is a second
# copy that can be consumed: two concurrent runs of this script each `Move-Item` the other's
# backup, and the run whose move loses then cannot restore at all - leaving the repository with the
# deliberately-broken documents, which is worse than not having the check. Restoring from a string
# cannot fail that way. (Concurrent runs are still unsupported, because the mutation itself is
# global: both would rewrite the same three files.)
$originals = @{}
foreach ($path in $mutatedPaths) {
    $originals[$path] = [System.IO.File]::ReadAllText($path)
}
# Appended as UTF-8 without a byte-order mark: `Add-Content -Encoding utf8` on PowerShell 5.1
# writes a BOM, and a BOM in the middle of a Markdown file is a stray character that the checker
# would then have to be taught to ignore.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
try {
    # The phantom endpoint is deliberately a single segment. That is the shape of the drift this
    # task found (`GET /sources`, `GET /topics`), and it is the shape an earlier "two or more
    # slashes" heuristic silently excused.
    [System.IO.File]::AppendAllText($mutatedPaths[0], "`n### " + '`GET /t18-not-a-real-endpoint`' + "`n", $utf8NoBom)
    [System.IO.File]::AppendAllText($mutatedPaths[1], "`n### " + '`zz_fake_table`' + "`n", $utf8NoBom)
    # Unticking the last task is the same act as forgetting to update the two status lines, so
    # this mutation is what proves the status check is reading the task list rather than a number
    # somebody keeps in step by hand.
    $architecture = [System.IO.File]::ReadAllText($mutatedPaths[2])
    [System.IO.File]::WriteAllText(
        $mutatedPaths[2],
        $architecture.Replace('- [x] T18 ', '- [ ] T18 '),
        $utf8NoBom
    )

    $brokenOutput = & $Python $driftScript 2>&1
    $brokenExit = $LASTEXITCODE
    $brokenText = $brokenOutput -join "`n"
    Assert-Equal $brokenExit 1 'a contract that promises an unregistered endpoint fails the check'
    Assert-True ($brokenText -match 'documented but not registered: GET /t18-not-a-real-endpoint') 'the drift names the endpoint the contract invented'
    Assert-True ($brokenText -match 'documented but not implemented: zz_fake_table') 'the drift names the table the data model invented'
    Assert-True ($brokenText -match 'does not name T17') 'unticking the last task makes both status lines stale'
    # Three independent comparisons, and the checker has to report all of them rather than
    # stopping at the first: one that reports a single finding per run turns a five-item drift
    # into five runs.
    Assert-True ($brokenText -match '\[endpoints\]') 'the endpoint comparison is one of the ones that failed'
    Assert-True ($brokenText -match '\[tables\]') 'the table comparison failed as well'
    Assert-True ($brokenText -match '\[status-lines\]') 'the status comparison failed as well'
}
finally {
    foreach ($path in $mutatedPaths) {
        [System.IO.File]::WriteAllText($path, $originals[$path], $utf8NoBom)
    }
}

$restoredOutput = & $Python $driftScript 2>&1
Assert-LastCommandSucceeded 'the documentation set is consistent again after the mutations were undone'
$restoredJson = $restoredOutput | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1
if (-not $restoredJson) {
    throw "the documentation checker produced no JSON payload after restore: $restoredOutput"
}
Assert-True ($restoredJson | ConvertFrom-Json).passed 'the checker passes once the documents are put back'

# ---------------------------------------------------------------------------------------
# Runtime evidence for the two endpoint groups T18 kept.
# ---------------------------------------------------------------------------------------

if ($SqliteFixture) {
    Write-Host "==> Prepare the SQLite fixture at $fixtureDir"
    New-Item -ItemType Directory -Force -Path $fixtureDir | Out-Null
    # `_env_file=None` is load-bearing: without it the fixture would read the developer's `.env`,
    # and a filled-in provider would turn this into a live, billable, non-reproducible run.
    $sitecustomize = @'
"""Repoint the T18 smoke at a SQLite file, standing in for an unavailable MySQL container."""

from __future__ import annotations

import os

from sqlalchemy import event

from open_source_intelligence.core import config as config_module

_SQLITE_URL = os.environ.get("T18_SQLITE_URL")

if _SQLITE_URL:
    _settings = config_module.Settings(
        _env_file=None,
        database_url="mysql+pymysql://app:secret@127.0.0.1:3306/open_source_intelligence",
        redis_url="redis://127.0.0.1:6379/0",
        secret_key="t18-fixture-secret-key-long-enough-for-validation",
        app_env="test",
        agent_provider="offline",
        push_delivery="offline",
    )
    _settings = _settings.model_copy(update={"database_url": _SQLITE_URL})

    config_module.get_settings = lambda: _settings  # type: ignore[assignment]
    config_module.clear_settings_cache = lambda: None  # type: ignore[assignment]

    from open_source_intelligence.persistence import session as session_module

    _create_engine = session_module.create_database_engine

    def _create_database_engine(database_url: str, *, echo: bool = False):
        engine = _create_engine(database_url, echo=echo)
        if database_url.startswith("sqlite"):

            @event.listens_for(engine, "connect")
            def _enable_foreign_keys(dbapi_connection, _record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        return engine

    session_module.create_database_engine = _create_database_engine  # type: ignore[assignment]
'@
    Set-Content -LiteralPath (Join-Path $fixtureDir 'sitecustomize.py') -Value $sitecustomize -Encoding utf8

    $createSchema = @'
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.base import Base
from open_source_intelligence.persistence.session import create_database_engine

engine = create_database_engine(get_settings().database_url)
Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
print("schema ready:", len(Base.metadata.tables), "tables")
'@
    Set-Content -LiteralPath (Join-Path $fixtureDir 'create_schema.py') -Value $createSchema -Encoding utf8

    $env:T18_SQLITE_URL = "sqlite+pysqlite:///$($fixtureDb -replace '\\', '/')"
    $env:PYTHONPATH = "$fixtureDir;$projectRoot\src;$projectRoot\scripts"

    & $Python (Join-Path $fixtureDir 'create_schema.py')
    Assert-LastCommandSucceeded 'SQLite fixture schema'

    Write-Host '==> Run the T18 documentation-sync smoke on the SQLite fixture'
    $smokeOutput = & $Python (Join-Path $projectRoot 'scripts\t18_mysql_smoke.py')
    Assert-LastCommandSucceeded 'T18 smoke'
}
else {
    Write-Host '==> Validate the Compose file'
    docker @composeArgs config --quiet
    Assert-LastCommandSucceeded 'docker compose config'

    Write-Host '==> Rebuild and start services'
    docker @composeArgs up -d --build --force-recreate --remove-orphans
    Assert-LastCommandSucceeded 'docker compose up'

    # T18 adds no migration, so the round trip belongs to its predecessors; what it still has to
    # prove here is that the schema it runs against is the one the migrations build.
    docker @composeArgs exec -T api alembic upgrade head
    Assert-LastCommandSucceeded 'alembic upgrade head'
    docker @composeArgs exec -T api alembic check
    Assert-LastCommandSucceeded 'alembic check'

    Write-Host '==> Run the T18 documentation-sync smoke on MySQL'
    $smokeOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t18_mysql_smoke.py
    Assert-LastCommandSucceeded 'T18 MySQL smoke'
}

# The smoke drives the real application, so its stdout also carries the application log; the
# payload is the last JSON object it prints.
$smokeJson = $smokeOutput |
    Where-Object { $_.TrimStart().StartsWith('{') } |
    Select-Object -Last 1
if (-not $smokeJson) {
    throw "the T18 smoke produced no JSON payload: $smokeOutput"
}
$smoke = $smokeJson | ConvertFrom-Json

$catalog = $smoke.catalog
$snapshots = $smoke.snapshots
$audit = $smoke.audit
$documentation = $smoke.documentation
$expected = $smoke.expected

# ---------------------------------------------------------------------------------------
# Catalog: the shared list and one repository's trend, over real rows.
# ---------------------------------------------------------------------------------------

# Criterion 1: the catalog is public but not anonymous, and the slice this smoke owns is exactly
# three rows - the `q` filter is what makes the assertion exact on a database that other drills
# have also written to.
Assert-Equal $catalog.anonymous_status 401 'an anonymous read of the catalog is refused'
Assert-Equal $catalog.total 3 'the catalog slice holds the three drill repositories'

# Criterion 2: the order is the one that was asked for, and it cannot be alphabetical by
# accident. `beta` / `gamma` / `alpha` is neither the alphabetical order nor the insertion order.
Assert-Equal ($catalog.full_names_ordered -join ',') '__t18_drill__/beta,__t18_drill__/gamma,__t18_drill__/alpha' 'the list is ordered by stars, not by name'
Assert-Equal $catalog.paging_overlap 0 'two pages of the same list share no row'

# Criterion 3: `provider` comes from the joined source row rather than from a constant, which is
# why one of the three rows sits on the second source.
Assert-Equal ($catalog.providers -join ',') 'github,huggingface' 'the list spans both configured providers'
Assert-Equal $catalog.gamma_provider 'huggingface' 'each row reports the provider of its own source'

# Criterion 4: without a `topic_id` no ranking was ever computed, so the three rank fields are
# null. Zero and false would be assertions about a sort that never happened.
$rankValues = @()
foreach ($row in $catalog.unranked_fields) {
    $rankValues += $row
}
Assert-Equal $rankValues.Count 9 'every row reports all three rank fields'
Assert-Equal @($rankValues | Where-Object { $null -ne $_ }).Count 0 'an unsliced list reports no score, rank, or selection rather than zero'

# Criterion 5: each filter narrows the total as well as the page. A filter applied after paging
# would return the same items and a different total, so the total is the half that has teeth.
Assert-Equal $catalog.language_filter_total 2 'the language filter is case-insensitive'
Assert-Equal $catalog.min_stars_total 1 'the star floor excludes the two smaller repositories'
Assert-Equal $catalog.description_query_total 1 'the text filter reaches the description as well as the name'
Assert-Equal $catalog.future_created_after_total 0 'a first-seen floor in the future matches nothing'
Assert-Equal $catalog.alpha_language $null 'a repository with no language reports null'
Assert-Equal ($catalog.alpha_topics -join ',') 'drill' 'the repository topics round-trip as a list'

# Criterion 6: an unknown ordering, an over-large page, and a timestamp that does not say which
# zone it is in are all refused at the boundary - the last one is the T12 defect T18 found, which
# used to surface as a 500 from inside the query builder.
Assert-Equal $catalog.unknown_sort_status 422 'an unknown sort is refused rather than silently ignored'
Assert-Equal $catalog.page_size_cap_status 422 'a page larger than the cap is refused'
Assert-Equal $catalog.naive_timestamp_status 422 'a timestamp without a timezone offset is refused at the edge'
Assert-True ($catalog.naive_timestamp_detail -match 'timezone offset') 'the refusal says which field is wrong and why'

# Criterion 7: the ranked slice carries the ranking, and the score leaves as text so the value
# that comes back is the value that was stored.
Assert-Equal ($catalog.ranked_ranks -join ',') '1,2,3' 'the ranked slice carries every candidate rank'
Assert-Equal $catalog.ranked_score_of_alpha '0.900000' 'a NUMERIC score round-trips exactly'
Assert-Equal ($catalog.ranked_selected -join ',') 'True,False,True' 'selection follows the topic ordering, not the star order'

# Criterion 8: the one filter that carries an authorization decision. A disabled topic is
# unpublished configuration: refused to a viewer, served to an administrator, and the refusal says
# nothing about the topic it refused.
Assert-Equal $catalog.closed_topic_status 403 'a viewer may not slice the catalog by a disabled topic'
Assert-Equal $catalog.closed_topic_leaks_name $false 'the refusal names no topic'
Assert-Equal $catalog.closed_topic_admin_status 200 'an administrator may slice by a disabled topic'
Assert-Equal $catalog.closed_topic_admin_total 3 'the administrator gets the same candidates'
Assert-Equal $catalog.missing_topic_status 404 'a topic that does not exist is a 404, not a 403'

# Criterion 9: the refusal is still on the record. `resource_id` is what makes the row usable:
# "someone was denied something" is not enough to investigate.
Assert-Equal $catalog.closed_topic_audit.count 1 'the refused slice leaves exactly one audit row'
Assert-Equal ($catalog.closed_topic_audit.resource_ids -join ',') "$($expected.closed_topic_id)" 'the refusal is filed against the topic it addressed'
Assert-Equal ($catalog.closed_topic_audit.results -join ',') 'denied' 'the refusal is recorded as a denial'

# Criterion 10: the trend window is closed on both ends - the only closed interval in the
# platform, because it cuts on calendar days rather than instants - and the first observation
# reports no delta rather than a zero.
Assert-Equal $snapshots.anonymous_status 401 'an anonymous read of a trend is refused'
Assert-Equal $snapshots.closed_window_dates.Count 3 'a three-day window returns all three days'
Assert-Equal ($snapshots.closed_window_dates -join ',') '2026-10-13,2026-10-14,2026-10-15' 'the window includes both of the dates it was given'
Assert-Equal $snapshots.first_day_delta $null 'the first observation has nothing to subtract from'
Assert-Equal $snapshots.second_day_delta 30 'the second observation reports the movement'
Assert-Equal $snapshots.single_day_count 1 'a single-day window returns that day'
Assert-Equal $snapshots.empty_window_status 200 'a window with no snapshots is empty, not missing'
Assert-Equal $snapshots.empty_window_count 0 'an empty window is an empty list'

# Criterion 11: the default window is the documented thirty days, and every way of asking for a
# window that cannot be served is refused rather than clamped.
Assert-Equal $snapshots.default_window_count 3 'the default window reaches the drill snapshots'
Assert-Equal $snapshots.default_window_days $expected.default_window_days 'the default window is thirty days wide'
Assert-Equal $snapshots.inverted_window_status 422 'a window that ends before it starts is refused'
Assert-Equal $snapshots.overlong_window_status 422 'a window longer than the cap is refused rather than shortened'
Assert-Equal $snapshots.unknown_granularity_status 422 'an unknown granularity is refused'
Assert-Equal $snapshots.missing_repository_status 404 'a repository that does not exist is a 404'

# ---------------------------------------------------------------------------------------
# Audit: the read-only view over the append-only trail.
# ---------------------------------------------------------------------------------------

# Criterion 12: reading the trail is itself a privilege, and the refusal is filed. The row is read
# from the database rather than through the API because a refusal the refused party can read
# about is not the same control.
Assert-Equal $audit.anonymous_status 401 'an anonymous read of the trail is refused'
Assert-Equal $audit.viewer_status 403 'a viewer may not read the audit trail'
Assert-Equal $audit.viewer_refusals 1 'the refused read leaves exactly one audit row'
Assert-Equal $audit.viewer_refusal_actor $expected.viewer_id 'the refusal names the caller it refused'
Assert-Equal $audit.viewer_refusal_resource_type 'auth' 'a role refusal is request-scoped and names no resource'

# Criterion 13: the trail comes back newest first, and the newest row is the refusal that just
# happened. `admin_total` is the stronger half: three seeded rows plus the administrator's own
# login, which means the endpoint is reading the ledger the application writes.
Assert-Equal $audit.admin_status 200 'an administrator may read the trail'
Assert-Equal $audit.newest_first $true 'the trail is served newest first'
Assert-Equal $audit.newest_action 'auth.rbac' 'the most recent event is the refusal just recorded'
Assert-Equal $audit.newest_actor $expected.viewer_id 'the most recent event names the viewer'
Assert-Equal $audit.admin_total 4 'the administrator''s own login is part of the ledger it reads'
Assert-Equal $audit.admin_newest_action 'auth.login' 'the administrator''s newest event is its login'

# Criterion 14: one column holds both IPv4 and IPv6, and it leaves as text. The row with no
# address at all is a second case, because a null and a zero-length blob are easy to conflate.
Assert-Equal $audit.ipv6_rendered '2001:db8::1' 'a stored IPv6 address is rendered as text'
Assert-Equal $audit.missing_ip_rendered $null 'a request with no client address reports null'

# Criterion 15: `metadata_json` is stored and does not leave. The sentinel is planted in the
# column itself, so this fails the moment the free-form field is wired to the response without
# anyone deciding what may travel in it.
Assert-Equal $audit.metadata_stored $true 'the free-form column really holds the sentinel'
Assert-Equal $audit.metadata_leaked $false 'neither the sentinel nor the column name appears in a response'

# Criterion 16: the result filter is a closed set, the page cap is enforced, and an unsayable
# timestamp is refused - the same boundary fix, on the second of the two new endpoints.
Assert-Equal $audit.unknown_result_status 422 'a result outside the CHECK constraint is refused'
Assert-Equal $audit.page_size_cap_status 422 'a page larger than the cap is refused'
Assert-Equal $audit.naive_timestamp_status 422 'a timestamp without a timezone offset is refused here too'

# ---------------------------------------------------------------------------------------
# The documentary verdict, produced by the same run.
# ---------------------------------------------------------------------------------------

Assert-True $documentation.passed 'the ten documentation comparisons pass inside the smoke too'
Assert-Equal $documentation.run 10 'every comparison ran'
# `drifted` is a comma-joined string rather than a list on purpose: `@($null).Count` is 1 in
# PowerShell, so `@(...).Count -eq 0` cannot tell an empty JSON array from an absent key.
Assert-Equal $documentation.drifted '' 'no comparison reported drift'

Write-Host '==> Leave the database as it was found'
if ($SqliteFixture) {
    & $Python (Join-Path $projectRoot 'scripts\t18_mysql_smoke.py') --cleanup-only | Out-Null
    Assert-LastCommandSucceeded 'T18 cleanup'
}
else {
    docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t18_mysql_smoke.py --cleanup-only | Out-Null
    Assert-LastCommandSucceeded 'T18 MySQL cleanup'
}

# `audited` prints the count, not the refused topic's `resource_ids`. The id is a database-assigned
# primary key: it is 13 on the Compose database and 2 on a freshly built fixture, so printing it
# would put one host-specific value in an otherwise engine-independent digest and invite exactly
# the misreading that the two payloads differ. That the row is filed against the addressed topic is
# asserted below, where it belongs.
Write-Host "T18 verification passed. catalog: total=$($catalog.total) order=$($catalog.full_names_ordered -join '/') providers=$($catalog.providers -join '+') unranked-nulls=$(@($rankValues | Where-Object { $null -ne $_ }).Count) disabled-topic=$($catalog.closed_topic_status)/$($catalog.closed_topic_admin_status) audited=$($catalog.closed_topic_audit.count); snapshots: window=$($snapshots.closed_window_dates.Count)d first-delta=$($snapshots.first_day_delta) second-delta=$($snapshots.second_day_delta) default=$($snapshots.default_window_days)d; audit: viewer=$($audit.viewer_status) refused=$($audit.viewer_refusals) admin-total=$($audit.admin_total) metadata-leaked=$($audit.metadata_leaked); documentation: run=$($documentation.run) passed=$($documentation.passed)."

[Console]::OutputEncoding = $previousOutputEncoding
