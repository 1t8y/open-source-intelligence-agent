"""提供离线评测的数据集、目标变体、确定性打分器与运行器。

对外接口刻意做得很小。调用方导入一个数据集版本，在其上运行一个或多个目标变体，再读回
一份统一的对比结果；本包之外的任何代码都不需要知道变体是怎么执行的、指标是怎么算出来的。
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
