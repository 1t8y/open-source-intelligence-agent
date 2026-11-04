"""提供带版本的评测数据集：文件格式、校验与幂等导入。

数据集在磁盘上是一份 JSON 文档，导入后落在 `eval_datasets` + `eval_cases` 两张表里。两条
规则保证结果可复现：

* `(name, version)` 就是身份。重复导入*相同*内容是一次空操作，报告 `replayed=True`；在既有
  版本号下导入*不同*内容会抛 `DatasetVersionConflict`，而不是覆盖。因此改动一个样本永远
  意味着升版本，也正是这一点让旧结果能追溯到产生它的那份样本集
  （`docs/evaluation.md` §2.3）。
* `input_json` 只携带目标运行所需的东西（主题名、窗口、标签）。人工参考答案与评分细则各占
  独立的一列，打分器因此绝不会把答案与问题混为一谈。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.evaluation import EvalCase, EvalDataset
from open_source_intelligence.persistence.models.intelligence import Topic

from .errors import (
    DatasetInvalid,
    DatasetTopicMissing,
    DatasetVersionConflict,
    EvaluationNotFound,
)

DATASET_SCHEMA_VERSION: Final[int] = 1
MAX_NAME_CHARS: Final[int] = 128
MAX_VERSION_CHARS: Final[int] = 32
MAX_DESCRIPTION_CHARS: Final[int] = 255
MAX_CASE_KEY_CHARS: Final[int] = 64
CASE_KEY_PREFIX: Final[str] = "case"

#: 未显式给出数据集路径时，CLI 查找的目录。
DEFAULT_DATASET_DIR: Final[str] = "datasets"


@dataclass(frozen=True, slots=True)
class DatasetCaseSpec:
    """一条校验通过的样本，尚未写入 `eval_cases` 行。"""

    case_key: str
    topic: str
    window_start: datetime
    window_end: datetime
    tags: tuple[str, ...] = ()
    notes: str | None = None
    reference: Mapping[str, Any] | None = None
    rubric: Mapping[str, Any] | None = None
    enabled: bool = True

    def to_input_json(self) -> dict[str, Any]:
        """渲染目标读取的运行上下文。"""
        payload: dict[str, Any] = {
            "topic": self.topic,
            "window": {
                "start": self.window_start.isoformat(),
                "end": self.window_end.isoformat(),
            },
            "tags": list(self.tags),
        }
        if self.notes:
            payload["notes"] = self.notes
        return payload


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """一份校验通过的数据集文档。"""

    name: str
    version: str
    description: str | None
    cases: tuple[DatasetCaseSpec, ...]

    def case_keys(self) -> tuple[str, ...]:
        """按文件顺序返回全部用例键。"""
        return tuple(case.case_key for case in self.cases)


@dataclass(frozen=True, slots=True)
class DatasetImportResult:
    """一次导入的结果：动到了哪一行数据集，以及它是不是新建的。"""

    dataset_id: int
    name: str
    version: str
    case_count: int
    created: bool
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        """返回可安全序列化为 JSON 的摘要。"""
        return {
            "dataset_id": self.dataset_id,
            "name": self.name,
            "version": self.version,
            "case_count": self.case_count,
            "created": self.created,
            "replayed": self.replayed,
        }


def _require_text(value: Any, *, field: str, limit: int) -> str:
    """要求一个非空字符串，且不超过列长度上限。"""
    if not isinstance(value, str) or not value.strip():
        raise DatasetInvalid(f"{field} must be a non-empty string")
    text = value.strip()
    if len(text) > limit:
        raise DatasetInvalid(f"{field} must be at most {limit} characters")
    return text


def _optional_text(value: Any, *, field: str, limit: int) -> str | None:
    """把缺失或空白的字符串当作未设置。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise DatasetInvalid(f"{field} must be a string when present")
    text = value.strip()
    if not text:
        return None
    if len(text) > limit:
        raise DatasetInvalid(f"{field} must be at most {limit} characters")
    return text


def _parse_datetime(value: Any, *, field: str) -> datetime:
    """解析 ISO-8601 时间戳，并要求显式给出时区。"""
    if not isinstance(value, str):
        raise DatasetInvalid(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DatasetInvalid(f"{field} is not a valid ISO-8601 timestamp: {value}") from exc
    if parsed.tzinfo is None:
        # 不带时区的窗口会静默依赖本机时钟，于是同一份数据集在不同主机上会给出不同结果。
        raise DatasetInvalid(f"{field} must include a timezone offset")
    return parsed


def _optional_mapping(value: Any, *, field: str) -> Mapping[str, Any] | None:
    """接受缺失值或一个 JSON 对象，其余一律拒绝。"""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise DatasetInvalid(f"{field} must be a JSON object when present")
    return value


def _parse_tags(value: Any, *, field: str) -> tuple[str, ...]:
    """接受缺失的标签列表，列表元素必须是非空字符串。"""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise DatasetInvalid(f"{field} must be a list of strings")
    tags: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise DatasetInvalid(f"{field} must contain non-empty strings")
        tags.append(item.strip())
    return tuple(tags)


def _parse_case(raw: Any, *, index: int) -> DatasetCaseSpec:
    """校验一条用例条目。"""
    if not isinstance(raw, Mapping):
        raise DatasetInvalid(f"cases[{index}] must be a JSON object")
    case_key = _require_text(raw.get("case_key"), field=f"cases[{index}].case_key", limit=64)
    if not all(char.isalnum() or char in "-._" for char in case_key):
        raise DatasetInvalid(
            f"cases[{index}].case_key must contain only letters, digits, '.', '-', '_'"
        )
    topic = _require_text(raw.get("topic"), field=f"cases[{index}].topic", limit=MAX_NAME_CHARS)
    window = raw.get("window")
    if not isinstance(window, Mapping):
        raise DatasetInvalid(f"cases[{index}].window must be an object with start and end")
    window_start = _parse_datetime(window.get("start"), field=f"cases[{index}].window.start")
    window_end = _parse_datetime(window.get("end"), field=f"cases[{index}].window.end")
    if window_start > window_end:
        raise DatasetInvalid(f"cases[{index}].window.start must not be after window.end")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise DatasetInvalid(f"cases[{index}].enabled must be a boolean")
    return DatasetCaseSpec(
        case_key=case_key,
        topic=topic,
        window_start=window_start,
        window_end=window_end,
        tags=_parse_tags(raw.get("tags"), field=f"cases[{index}].tags"),
        notes=_optional_text(raw.get("notes"), field=f"cases[{index}].notes", limit=1000),
        reference=_optional_mapping(raw.get("reference"), field=f"cases[{index}].reference"),
        rubric=_optional_mapping(raw.get("rubric"), field=f"cases[{index}].rubric"),
        enabled=enabled,
    )


def parse_dataset(document: Any) -> DatasetSpec:
    """校验一份已经解码的数据集文档。"""
    if not isinstance(document, Mapping):
        raise DatasetInvalid("dataset document must be a JSON object")
    schema_version = document.get("schema_version", DATASET_SCHEMA_VERSION)
    if schema_version != DATASET_SCHEMA_VERSION:
        raise DatasetInvalid(
            f"unsupported schema_version {schema_version!r}; expected {DATASET_SCHEMA_VERSION}"
        )
    name = _require_text(document.get("name"), field="name", limit=MAX_NAME_CHARS)
    version = _require_text(document.get("version"), field="version", limit=MAX_VERSION_CHARS)
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise DatasetInvalid("cases must be a non-empty list")
    cases = tuple(_parse_case(raw, index=index) for index, raw in enumerate(raw_cases))
    keys = [case.case_key for case in cases]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise DatasetInvalid(f"duplicate case_key(s): {', '.join(duplicates)}")
    return DatasetSpec(
        name=name,
        version=version,
        description=_optional_text(
            document.get("description"),
            field="description",
            limit=MAX_DESCRIPTION_CHARS,
        ),
        cases=cases,
    )


def load_dataset_file(path: str | Path) -> DatasetSpec:
    """读取并校验一个数据集 JSON 文件。"""
    resolved = Path(path)
    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise DatasetInvalid(f"cannot read dataset file {resolved}: {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DatasetInvalid(f"{resolved} is not valid JSON: {exc}") from exc
    return parse_dataset(document)


def discover_dataset_files(directory: str | Path = DEFAULT_DATASET_DIR) -> list[Path]:
    """返回目录下的数据集文件，并排序以保证导入的确定性。"""
    root = Path(directory)
    if not root.is_dir():
        return []
    return sorted(root.glob("*.json"))


def _canonical(value: Any) -> Any:
    """归一化 JSON 值，让两份相等的载荷在不同驱动下也比较相等。

    MySQL 把 JSON 数字读回来是 `Decimal`，SQLite 读回来是 `float`，而重放检查要拿刚解析
    出的文档去比已存的行。序列化之前把数字统一成 `float`，就把这条驱动差异从比较里抹掉了。
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_canonical(item) for item in value]
    return value


def _fingerprint(
    *,
    input_json: Mapping[str, Any],
    reference_json: Mapping[str, Any] | None,
    rubric_json: Mapping[str, Any] | None,
    enabled: bool,
) -> str:
    """返回一个稳定字符串，涵盖用例行存储的全部内容。"""
    return json.dumps(
        {
            "input": _canonical(input_json),
            "reference": _canonical(reference_json),
            "rubric": _canonical(rubric_json),
            "enabled": enabled,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _require_topics_for_cases(session: Session, spec: DatasetSpec) -> None:
    """拒绝导入那些*启用中*的用例引用了本部署所没有的主题的数据集。

    只检查启用中的用例，且只在新版本时检查。被禁用的用例是一个等数据就位再启用的场景，
    因此允许它点名一个尚无人创建的主题；而重放是一次内容检查，不是就绪检查，所以它不该
    额外多出一种失败方式。

    另一种做法 —— 先导入，等运行时才发现 —— 会产出一个每个变体都以
    `EVAL_TOPIC_NOT_FOUND` 失败的数据集，读起来像「模型坏了」，而不是「这个文件是写给另一个
    部署的」。
    """
    wanted = sorted({case.topic for case in spec.cases if case.enabled})
    if not wanted:
        return
    available = set(session.scalars(select(Topic.name).where(Topic.enabled.is_(True))).all())
    missing = [name for name in wanted if name not in available]
    if missing:
        raise DatasetTopicMissing(
            f"dataset {spec.name} version {spec.version} names topic(s) that are missing or "
            f"disabled here: {', '.join(repr(name) for name in missing)}; "
            f"enabled topics: {', '.join(repr(name) for name in sorted(available)) or '(none)'}"
        )


def import_dataset(
    session: Session,
    spec: DatasetSpec,
    *,
    created_by: int | None = None,
) -> DatasetImportResult:
    """导入一个数据集版本：内容相同则重放，内容冲突则拒绝。"""
    dataset = session.scalar(
        select(EvalDataset).where(
            EvalDataset.name == spec.name,
            EvalDataset.version == spec.version,
        )
    )
    if dataset is None:
        _require_topics_for_cases(session, spec)
        dataset = EvalDataset(
            name=spec.name,
            version=spec.version,
            description=spec.description,
            created_by=created_by,
        )
        session.add(dataset)
        session.flush()
        for case in spec.cases:
            session.add(_build_case(dataset.id, case))
        session.commit()
        return DatasetImportResult(
            dataset_id=dataset.id,
            name=spec.name,
            version=spec.version,
            case_count=len(spec.cases),
            created=True,
            replayed=False,
        )

    stored = {
        row.case_key: _fingerprint(
            input_json=row.input_json,
            reference_json=row.reference_json,
            rubric_json=row.rubric_json,
            enabled=row.enabled,
        )
        for row in session.scalars(select(EvalCase).where(EvalCase.dataset_id == dataset.id))
    }
    incoming = {
        case.case_key: _fingerprint(
            input_json=case.to_input_json(),
            reference_json=None if case.reference is None else dict(case.reference),
            rubric_json=None if case.rubric is None else dict(case.rubric),
            enabled=case.enabled,
        )
        for case in spec.cases
    }
    if stored != incoming:
        changed = sorted(set(stored) ^ set(incoming)) or sorted(
            key for key in stored if stored[key] != incoming.get(key)
        )
        raise DatasetVersionConflict(
            f"dataset {spec.name} version {spec.version} already exists with different content "
            f"(differing case(s): {', '.join(changed)}); bump the version instead of editing it"
        )
    return DatasetImportResult(
        dataset_id=dataset.id,
        name=spec.name,
        version=spec.version,
        case_count=len(incoming),
        created=False,
        replayed=True,
    )


def _build_case(dataset_id: int, case: DatasetCaseSpec) -> EvalCase:
    """把一条校验通过的用例转成 ORM 行。"""
    return EvalCase(
        dataset_id=dataset_id,
        case_key=case.case_key,
        input_json=case.to_input_json(),
        reference_json=None if case.reference is None else dict(case.reference),
        rubric_json=None if case.rubric is None else dict(case.rubric),
        enabled=case.enabled,
    )


def resolve_dataset(
    session: Session,
    *,
    dataset_id: int | None = None,
    name: str | None = None,
    version: str | None = None,
) -> EvalDataset:
    """按 id，或按名称加版本加载一个数据集，缺失时抛错。"""
    if dataset_id is not None:
        dataset = session.get(EvalDataset, dataset_id)
        if dataset is None:
            raise EvaluationNotFound(f"eval dataset {dataset_id} does not exist")
        return dataset
    if name is None or version is None:
        raise DatasetInvalid("either dataset_id or both name and version are required")
    dataset = session.scalar(
        select(EvalDataset).where(EvalDataset.name == name, EvalDataset.version == version)
    )
    if dataset is None:
        raise EvaluationNotFound(f"eval dataset {name} version {version} does not exist")
    return dataset


def list_datasets(session: Session, *, limit: int = 50) -> Sequence[EvalDataset]:
    """返回数据集，最新的在前。"""
    if limit < 1:
        raise DatasetInvalid("limit must be at least 1")
    return session.scalars(select(EvalDataset).order_by(EvalDataset.id.desc()).limit(limit)).all()


def list_cases(
    session: Session,
    *,
    dataset_id: int,
    enabled_only: bool = False,
) -> Sequence[EvalCase]:
    """按用例键顺序返回某个数据集的用例。"""
    statement = select(EvalCase).where(EvalCase.dataset_id == dataset_id)
    if enabled_only:
        statement = statement.where(EvalCase.enabled.is_(True))
    return session.scalars(statement.order_by(EvalCase.case_key.asc())).all()
