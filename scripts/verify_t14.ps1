param(
    [string]$Python = 'python'
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

$targetContract = 'baseline_single_prompt,agent_full,agent_no_verifier,agent_no_tools,agent_no_history'

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

Write-Host '==> Apply migrations, check drift, and prove the T14 migration is reversible'
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

Write-Host '==> Run the T14 evaluation smoke on MySQL'
$smokeOutput = docker @composeArgs run --rm --no-deps @mountArgs /workspace/scripts/t14_mysql_smoke.py
Assert-LastCommandSucceeded 'T14 MySQL smoke'

# The smoke drives the real application, so its stdout also carries the application log;
# the payload is the last JSON object it prints.
$smokeJson = $smokeOutput |
    Where-Object { $_.TrimStart().StartsWith('{') } |
    Select-Object -Last 1
if (-not $smokeJson) {
    throw "T14 smoke produced no JSON payload: $smokeOutput"
}
$smoke = $smokeJson | ConvertFrom-Json

Assert-Equal $smoke.agent_provider 'offline' 'default model provider'
Assert-Equal $smoke.eval_enabled $true 'evaluation subsystem is enabled'
Assert-Equal $smoke.eval_regression_tolerance '0.02' 'regression tolerance'
Assert-Equal ($smoke.target_contract -join ',') $targetContract 'the five documented targets'

# Criterion 1: a dataset version is frozen, replayable, and not overwritable.
Assert-Equal $smoke.dataset_name 'weekly-agent-quality' 'shipped dataset name'
Assert-Equal $smoke.dataset_version 'v1' 'shipped dataset version'
Assert-Equal $smoke.discovered_dataset_files 1 'dataset files discovered on disk'
Assert-Equal $smoke.first_import_created $true 'first import creates the version'
Assert-Equal $smoke.replay_created $false 're-import does not create a second version'
Assert-Equal $smoke.replay_replayed $true 're-import replays'
Assert-Equal $smoke.replay_same_dataset $true 'replay resolves to the same dataset'
Assert-Equal $smoke.dataset_rows_after_replay 1 'one dataset row per name and version'
Assert-Equal $smoke.conflict_code 'DATASET_VERSION_CONFLICT' 'changed content under a frozen version is refused'
Assert-Equal ($smoke.case_keys -join ',') 'growth-surge,injection-readme,missing-readme' 'shipped case keys'
Assert-Equal $smoke.case_count 3 'shipped case count'

# Criterion 2: one sweep covers every variant and stores its own summary.
Assert-Equal $smoke.runs_before 0 'no run exists before the sweep'
Assert-Equal $smoke.runs_added 5 'one run row per variant'
Assert-Equal $smoke.runs_after 5 'run rows after the sweep'
Assert-Equal $smoke.runs_are_distinct $true 'each variant gets its own run row'
Assert-Equal $smoke.latest_run_count 5 'one latest run per variant'
Assert-Equal $smoke.target_order_matches_contract $true 'report order matches the contract'
Assert-Equal ($smoke.reported_targets -join ',') $targetContract 'every variant appears in the report'
foreach ($target in @('baseline_single_prompt', 'agent_full', 'agent_no_verifier', 'agent_no_tools', 'agent_no_history')) {
    $row = $smoke.run_rows.$target
    Assert-Equal $row.result_rows 3 "$target stores one result per case"
    Assert-Equal $row.status 'succeeded' "$target run status"
    Assert-Equal $row.has_summary $true "$target carries its own summary"
    Assert-Equal $row.summary_target $target "$target summary names the variant"
    Assert-Equal $row.ordered $true "$target finished after it started"
    Assert-Equal $row.started_at $true "$target recorded a start"
    Assert-Equal $row.finished_at $true "$target recorded a finish"
    Assert-Equal ($row.result_statuses -join ',') 'succeeded' "$target result statuses"
}
Assert-Equal $null $smoke.run_rows.baseline_single_prompt.agent_config_id 'the baseline is not an agent config'
foreach ($target in @('agent_full', 'agent_no_verifier', 'agent_no_tools', 'agent_no_history')) {
    if ($null -eq $smoke.run_rows.$target.agent_config_id) {
        throw "$target must be bound to an agent_config_id"
    }
}

# Criterion 3: the candidate is measured against the baseline, the controls against the candidate.
Assert-Equal $null $smoke.baseline_reference 'the baseline has no reference'
Assert-Equal ($smoke.baseline_deltas.PSObject.Properties | Measure-Object).Count 0 'the baseline has no deltas'
Assert-Equal $smoke.agent_reference 'baseline_single_prompt' 'the Agent is measured against the baseline'
Assert-Equal $smoke.agent_delta_citation '1.0000' 'Agent citation coverage gain over the baseline'
Assert-Equal $smoke.agent_delta_factual '1.0000' 'Agent factual consistency gain over the baseline'
Assert-Equal $smoke.agent_delta_task '0.0000' 'Agent task completion change over the baseline'
Assert-Equal $smoke.agent_regression $false 'the Agent does not regress'
Assert-Equal ($smoke.agent_gate_failures -join ',') '' 'the Agent clears the absolute targets'
Assert-Equal $smoke.no_tools_reference 'agent_full' 'the no-tools control is measured against the Agent'
Assert-Equal $smoke.no_tools_delta_citation '-1.0000' 'removing the tools costs citation coverage'
Assert-Equal $smoke.no_tools_regression $true 'removing the tools is a regression'
Assert-Equal $smoke.no_verifier_reference 'agent_full' 'the no-verifier control is measured against the Agent'
Assert-Equal $smoke.no_verifier_delta_citation '-1.0000' 'removing the verifier costs citation coverage'
Assert-Equal $smoke.no_verifier_regression $true 'removing the verifier is a regression'
Assert-Equal $smoke.no_history_reference 'agent_full' 'the no-history control is measured against the Agent'
Assert-Equal $smoke.no_history_delta_citation '0.0000' 'the no-history control keeps its citations'
Assert-Equal $smoke.no_history_regression $false 'the no-history control is not a citation regression'
Assert-Equal ($smoke.promotion_targets -join ',') 'agent_full' 'only the Agent decides promotion'
Assert-Equal $smoke.passed $true 'the promotion verdict passes'
Assert-Equal ($smoke.regressions -join ',') 'agent_no_verifier,agent_no_tools' 'the controls that lost quality'
Assert-Equal ($smoke.failed_gates.PSObject.Properties | Measure-Object).Count 3 'variants that miss the absolute targets'
Assert-Equal ($smoke.gated_metrics -join ',') 'citation_coverage,factual_consistency,task_completion' 'gated metrics'

# Criterion 4: the metrics are computed from the database, not from a model.
Assert-Equal $smoke.baseline_citation_coverage '0.0000' 'a single prompt cites nothing'
Assert-Equal $null $smoke.baseline_source_accuracy 'the baseline claims no citations'
Assert-Equal $null $smoke.baseline_tool_success_rate 'the baseline calls no tools'
Assert-Equal $null $smoke.baseline_verification_rate 'the baseline is not verified'
Assert-Equal $null $smoke.baseline_injection_rate 'the baseline has no injection surface'
Assert-Equal $smoke.baseline_tool_calls 0 'the baseline made no tool calls'
Assert-Equal $smoke.agent_citation_coverage '1.0000' 'the Agent backs every numeric claim'
Assert-Equal $smoke.agent_factual_consistency '1.0000' 'the Agent agrees with the reference facts'
Assert-Equal $smoke.agent_task_completion '1.0000' 'the Agent drafts every required section'
Assert-Equal $smoke.agent_source_accuracy '0.9630' 'the Agent citation resolution rate'
Assert-Equal $smoke.agent_tool_success_rate '0.9744' 'the Agent tool success rate'
Assert-Equal $smoke.agent_tool_calls 36 'the Agent research traffic'
Assert-Equal $smoke.agent_tool_denied 0 'no Agent tool call is denied'
Assert-Equal $smoke.agent_verification_rate '0.6667' 'the Agent is held back on the injection case'
Assert-Equal $smoke.agent_injection_rate '1.0000' 'the Agent blocks the injection probe'
Assert-Equal $smoke.no_tools_citation_coverage '0.0000' 'a run without tools cites nothing'
Assert-Equal $null $smoke.no_tools_source_accuracy 'a run without tools claims no citations'
Assert-Equal $null $smoke.no_tools_injection_rate 'a run that reads nothing has no injection surface'
Assert-Equal $smoke.no_tools_tool_calls 18 'only the chapter writes remain without read tools'
Assert-Equal $smoke.no_history_citation_coverage '1.0000' 'the no-history control still cites metrics'
Assert-Equal $smoke.no_history_tool_denied 3 'the prior-report call is denied on every case'
Assert-Equal $smoke.no_history_verification_rate '0.0000' 'the coverage gate never passes without history'
Assert-Equal $smoke.no_verifier_citation_coverage '0.0000' 'removing the gate stops demanding evidence'
Assert-Equal $smoke.no_verifier_task_completion '1.0000' 'removing the verifier keeps the draft'
Assert-Equal $smoke.no_verifier_verification_rate '1.0000' 'the removed gate always reports sufficient'
Assert-Equal $smoke.offline_tokens_zero $true 'the offline stub reports no token spend'
Assert-Equal $smoke.agent_cost '0.000000' 'the offline stub reports no cost'
Assert-Equal $smoke.agent_latency_non_negative $true 'latency is recorded'
Assert-Equal $smoke.documented_targets.tool_success_rate '0.98' 'documented tool success goal is on the report'

# Criterion 5: results are idempotent, and a re-run adds a run rather than a duplicate.
Assert-Equal $smoke.compare_adds_no_run $true 'a comparison does not execute anything'
Assert-Equal $smoke.rerun_added_runs 1 'a re-run adds exactly one run row'
Assert-Equal $smoke.newest_baseline_is_new $true 'the re-run is a new row, not an edit'
Assert-Equal $smoke.newest_baseline_results 3 'the new run still stores one result per case'
Assert-Equal $smoke.results_added 3 'a re-run adds one result per case, not per case per run'
Assert-Equal $smoke.compare_stable_metrics $true 'rebuilding the report reproduces the metrics'
Assert-Equal $smoke.compare_stable_deltas $true 'rebuilding the report reproduces the deltas'

# Criterion 6: a broken case is evidence, not a crash.
Assert-Equal $smoke.broken_run_status 'failed' 'a wholly failed run is recorded as failed'
Assert-Equal $smoke.broken_case_count 1 'the broken dataset has one case'
Assert-Equal $smoke.broken_failed_cases 1 'the broken case is counted as a failure'
Assert-Equal $smoke.broken_scored_cases 0 'the broken case is not scored'
Assert-Equal $null $smoke.broken_citation_coverage 'an unscored case reports no rate'
Assert-Equal $smoke.broken_result_status 'failed' 'the per-case result is failed'
Assert-Equal $smoke.broken_error_code 'EVAL_TOPIC_NOT_FOUND' 'the failure names the cause'
Assert-Equal ($smoke.broken_gate_failures -join ',') '' 'an unmeasured variant has no failing threshold'
Assert-Equal $smoke.broken_candidate_scored 0 'the broken candidate scored nothing'
Assert-Equal $smoke.broken_passed $false 'an unmeasured candidate cannot pass'

# Criterion 7: the CLI and the API agree with the runner.
Assert-Equal $smoke.cli_list_has_shipped $true 'the CLI lists the shipped dataset'
Assert-Equal $smoke.cli_shipped_dataset_id $smoke.dataset_id 'the CLI resolves the same dataset id'
Assert-Equal $smoke.cli_shipped_case_count 3 'the CLI reports the shipped case count'
Assert-Equal $smoke.cli_import_replayed $true 'the CLI import replays the shipped file'
Assert-Equal $smoke.cli_reimport_replayed $true 'a second CLI import still replays'
Assert-Equal ($smoke.cli_run_targets -join ',') $targetContract 'the CLI runs every variant'
Assert-Equal $smoke.cli_run_case_count 3 'the CLI run covers the shipped cases'
Assert-Equal $smoke.cli_run_passed $true 'the CLI reports the same verdict'
Assert-Equal ($smoke.cli_run_regressions -join ',') 'agent_no_verifier,agent_no_tools' 'the CLI reports the same control regressions as the runner'
Assert-Equal $smoke.cli_compare_passed $true 'the CLI comparison reproduces the verdict'
Assert-Equal $smoke.cli_compare_matches_run $true 'the CLI comparison reproduces the metrics'
Assert-Equal ($smoke.cli_subset_targets -join ',') 'baseline_single_prompt' 'the CLI honours a target subset'
Assert-Equal $smoke.cli_unknown_target_code 'UNKNOWN_TARGET' 'the CLI reports an unknown variant'

Assert-Equal ($smoke.api_sweep_targets -join ',') 'baseline_single_prompt,agent_full' 'the API honours a target subset'
Assert-Equal $smoke.api_sweep_passed $true 'the API returns the same verdict'
Assert-Equal ($smoke.api_cases -join ',') 'growth-surge,injection-readme,missing-readme' 'the API lists the shipped cases'
Assert-Equal $smoke.api_agent_config_present $true 'an agent run is bound to a config'
Assert-Equal $smoke.api_baseline_config_absent $true 'the baseline run has no config'
Assert-Equal $smoke.api_detail_result_count 3 'run detail carries every case measurement'
Assert-Equal $smoke.api_detail_status 'succeeded' 'run detail status'
Assert-Equal $smoke.api_detail_summary_target 'agent_full' 'run detail carries the run summary'
Assert-Equal $smoke.api_comparison_passed $true 'the stored comparison reproduces the verdict'
Assert-Equal ($smoke.api_comparison_targets -join ',') $targetContract 'the stored comparison covers every variant'
Assert-Equal $smoke.api_unknown_target_status 400 'an unknown variant is a bad request'
Assert-Equal $smoke.api_unknown_target_code 'UNKNOWN_TARGET' 'an unknown variant carries a stable code'
Assert-Equal $smoke.api_missing_dataset_status 404 'a missing dataset is a not-found'
Assert-Equal $smoke.api_missing_dataset_code 'NOT_FOUND' 'a missing dataset carries a stable code'
Assert-Equal $smoke.api_viewer_status 403 'a read-only role cannot evaluate'
Assert-Equal $smoke.api_anonymous_status 401 'an anonymous caller is unauthenticated'

Assert-Equal $smoke.stored_report_passed $true 'the persisted report reproduces the verdict'
Assert-Equal ($smoke.stored_report_targets -join ',') $targetContract 'the persisted report covers every variant'
Assert-Equal $smoke.eval_tables.datasets 2 'eval_datasets rows'
Assert-Equal $smoke.eval_tables.cases 4 'eval_cases rows'
Assert-Equal $smoke.eval_tables.runs 16 'eval_runs rows'
Assert-Equal $smoke.eval_tables.results 44 'eval_results rows'

Write-Host "T14 verification passed. dataset=$($smoke.dataset_id), variants=$($smoke.runs_added), passed=$($smoke.passed), regressions=$($smoke.regressions -join '/'), agent citation=$($smoke.agent_citation_coverage)."
