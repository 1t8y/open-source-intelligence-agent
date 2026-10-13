"""Offline evaluation: datasets, target variants, deterministic scorers, and the runner.

The public surface is deliberately small. Callers import a dataset version, run one or more
target variants over it, and read back a unified comparison; nothing outside this package needs
to know how a variant is executed or how a metric is computed.
"""

from .dataset import (
    DatasetCaseSpec,
    DatasetImportResult,
    DatasetSpec,
    discover_dataset_files,
    import_dataset,
    list_cases,
    list_datasets,
    load_dataset_file,
    parse_dataset,
    resolve_dataset,
)
from .errors import (
    DatasetInvalid,
    DatasetVersionConflict,
    EvaluationDisabled,
    EvaluationError,
    EvaluationNotFound,
    UnknownTarget,
)
from .runner import (
    GATED_METRICS,
    METRIC_TARGETS,
    ComparisonReport,
    TargetSummary,
    aggregate_run,
    build_comparison,
    collect_datasets,
    compare_latest,
    latest_runs_per_target,
    list_runs,
    resolve_targets,
    run_evaluation,
)
from .scorers import ScoredCase, score_case, score_failed_case
from .targets import (
    BASELINE_TARGET,
    TARGET_ORDER,
    SectionDraft,
    VariantOutput,
    run_target,
)

__all__ = [
    "BASELINE_TARGET",
    "GATED_METRICS",
    "METRIC_TARGETS",
    "TARGET_ORDER",
    "ComparisonReport",
    "DatasetCaseSpec",
    "DatasetImportResult",
    "DatasetInvalid",
    "DatasetSpec",
    "DatasetVersionConflict",
    "EvaluationDisabled",
    "EvaluationError",
    "EvaluationNotFound",
    "ScoredCase",
    "SectionDraft",
    "TargetSummary",
    "UnknownTarget",
    "VariantOutput",
    "aggregate_run",
    "build_comparison",
    "collect_datasets",
    "compare_latest",
    "discover_dataset_files",
    "import_dataset",
    "latest_runs_per_target",
    "list_cases",
    "list_datasets",
    "list_runs",
    "load_dataset_file",
    "parse_dataset",
    "resolve_dataset",
    "resolve_targets",
    "run_evaluation",
    "run_target",
    "score_case",
    "score_failed_case",
]
