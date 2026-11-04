"""提供导入数据集、运行评测与比较结果的命令行入口。

`--action run` 是真正执行 Agent 的那一个；`list` 与 `compare` 是只读的，存在的目的是让
运维不碰 API 也能查看某个数据集及其最近一次比较。每个动作都打印一份 JSON 文档，与本代码库
其它 CLI 保持一致。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sqlalchemy.orm import Session

from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)

from .dataset import (
    DEFAULT_DATASET_DIR,
    DatasetSpec,
    discover_dataset_files,
    import_dataset,
    list_cases,
    list_datasets,
    load_dataset_file,
)
from .errors import DatasetTopicMissing, EvaluationError
from .runner import compare_latest, run_evaluation

ACTIONS = ("list", "import", "run", "compare")


def parse_args() -> argparse.Namespace:
    """解析动作、其数据集以及可选的目标选择。"""
    parser = argparse.ArgumentParser(description="Import and run offline evaluations")
    parser.add_argument("--action", choices=ACTIONS, default="list")
    parser.add_argument("--dataset-file")
    parser.add_argument("--dataset-dir", default=DEFAULT_DATASET_DIR)
    parser.add_argument("--dataset-id", type=int)
    parser.add_argument("--name")
    parser.add_argument("--version")
    parser.add_argument(
        "--target",
        action="append",
        dest="targets",
        help="repeatable; defaults to every target variant",
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--actor-id", type=int)
    return parser.parse_args()


def _load_specs(args: argparse.Namespace) -> list[DatasetSpec]:
    """返回待导入的数据集文档。"""
    if args.dataset_file:
        return [load_dataset_file(args.dataset_file)]
    paths = discover_dataset_files(args.dataset_dir)
    if not paths:
        raise SystemExit(f"no dataset file found in {args.dataset_dir!r}")
    return [load_dataset_file(path) for path in paths]


def run_action(args: argparse.Namespace, session: Session) -> dict[str, Any]:
    """在给定会话上执行一个 CLI 动作。"""
    if args.action == "list":
        if args.limit < 1:
            raise SystemExit("--limit must be at least 1")
        return {
            "items": [
                {
                    "dataset_id": row.id,
                    "name": row.name,
                    "version": row.version,
                    "description": row.description,
                    "case_count": len(list_cases(session, dataset_id=row.id)),
                }
                for row in list_datasets(session, limit=args.limit)
            ]
        }
    if args.action == "import":
        imported: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        # 目录模式的含义是「把这里适用的东西全部导入」；在这个模式下，为别的部署写的数据集
        # 不算错误，只是不适用。显式点名一个文件则正相反 —— 运维要的就是那个文件，所以主题
        # 缺失时必须大声报错。
        directory_mode = args.dataset_file is None
        for spec in _load_specs(args):
            try:
                imported.append(import_dataset(session, spec, created_by=args.actor_id).to_dict())
            except DatasetTopicMissing as exc:
                if not directory_mode:
                    raise
                skipped.append(
                    {
                        "name": spec.name,
                        "version": spec.version,
                        "code": exc.code,
                        "reason": str(exc),
                    }
                )
        payload: dict[str, Any] = {"imported": imported}
        if directory_mode:
            payload["skipped"] = skipped
        return payload
    if args.action == "run":
        report = run_evaluation(
            session,
            dataset_id=args.dataset_id,
            name=args.name,
            version=args.version,
            target_types=args.targets,
        )
        return report.to_dict()
    if args.dataset_id is None:
        raise SystemExit("--dataset-id is required for the compare action")
    return compare_latest(session, dataset_id=args.dataset_id).to_dict()


def main() -> None:
    """执行一个动作并打印 JSON 摘要。"""
    args = parse_args()
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            payload = run_action(args, session)
    except EvaluationError as exc:
        # 稳定的 `code` 让脚本可以按失败类型分支，不必去匹配消息文本，与 API 的应答方式
        # 完全一致。
        print(
            json.dumps(
                {"action": args.action, "code": exc.code, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        engine.dispose()
        sys.exit(1)
    engine.dispose()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
