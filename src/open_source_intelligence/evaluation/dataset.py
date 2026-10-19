"""Versioned evaluation datasets: file format, validation, and idempotent import.

A dataset lives on disk as one JSON document and is imported into
`eval_datasets` + `eval_cases`. Two rules make results reproducible:

* `(name, version)` is the identity. Re-importing the *same* content is a no-op that reports
  `replayed=True`; importing *different* content under an existing version raises
  `DatasetVersionConflict` instead of overwriting. Changing a sample therefore always means
  bumping the version, which is what keeps an old result traceable to the exact sample set
  that produced it (`docs/evaluation.md` §2.3).
* `input_json` carries only what a target needs to run (topic name, window, tags). The human
  reference and the rubric are separate columns so a scorer can never confuse the answer with
  the question.
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

#: Directory the CLI looks in when no explicit dataset path is given.
DEFAULT_DATASET_DIR: Final[str] = "datasets"


@dataclass(frozen=True, slots=True)
class DatasetCaseSpec:
    """One validated sample, before it is turned into an `eval_cases` row."""

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
        """Render the run context that a target reads."""
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
    """One validated dataset document."""

    name: str
    version: str
    description: str | None
    cases: tuple[DatasetCaseSpec, ...]

    def case_keys(self) -> tuple[str, ...]:
        """Return every case key in file order."""
        return tuple(case.case_key for case in self.cases)


@dataclass(frozen=True, slots=True)
class DatasetImportResult:
    """Outcome of one import: which dataset row was touched and whether it was new."""

    dataset_id: int
    name: str
    version: str
    case_count: int
    created: bool
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe summary."""
        return {
            "dataset_id": self.dataset_id,
            "name": self.name,
            "version": self.version,
            "case_count": self.case_count,
            "created": self.created,
            "replayed": self.replayed,
        }


def _require_text(value: Any, *, field: str, limit: int) -> str:
    """Require a non-empty string within the column limit."""
    if not isinstance(value, str) or not value.strip():
        raise DatasetInvalid(f"{field} must be a non-empty string")
    text = value.strip()
    if len(text) > limit:
        raise DatasetInvalid(f"{field} must be at most {limit} characters")
    return text


def _optional_text(value: Any, *, field: str, limit: int) -> str | None:
    """Accept an absent or blank string as unset."""
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
    """Parse an ISO-8601 timestamp, requiring an explicit timezone."""
    if not isinstance(value, str):
        raise DatasetInvalid(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DatasetInvalid(f"{field} is not a valid ISO-8601 timestamp: {value}") from exc
    if parsed.tzinfo is None:
        # A naive window would silently depend on the machine's clock, which would make the
        # same dataset produce different results on different hosts.
        raise DatasetInvalid(f"{field} must include a timezone offset")
    return parsed


def _optional_mapping(value: Any, *, field: str) -> Mapping[str, Any] | None:
    """Accept an absent value or a JSON object, rejecting everything else."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise DatasetInvalid(f"{field} must be a JSON object when present")
    return value


def _parse_tags(value: Any, *, field: str) -> tuple[str, ...]:
    """Accept an absent list of non-empty strings."""
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
    """Validate one case entry."""
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
    """Validate one already-decoded dataset document."""
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
    """Read and validate one dataset JSON file."""
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
    """Return the dataset files under a directory, sorted for deterministic imports."""
    root = Path(directory)
    if not root.is_dir():
        return []
    return sorted(root.glob("*.json"))


def _canonical(value: Any) -> Any:
    """Normalize a JSON value so two equal payloads compare equal across drivers.

    MySQL hands JSON numbers back as `Decimal` while SQLite hands them back as `float`, and
    the replay check compares a freshly parsed document against a stored row. Normalizing
    numbers to `float` before serializing removes that driver difference from the comparison.
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
    """Return a stable string that captures everything a case row stores."""
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
    """Refuse an import whose *enabled* cases name topics this deployment does not have.

    Only enabled cases are checked, and only when the version is new. A disabled case is a
    scenario parked until its data exists, so it is allowed to name a topic nobody has yet; and a
    replay is a content check, not a readiness check, so it must not acquire a new way to fail.

    The alternative - importing and discovering it at run time - produces a dataset whose every
    variant fails with `EVAL_TOPIC_NOT_FOUND`, which reads like "the model is broken" rather than
    "this file was written for a different deployment".
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
    """Import a dataset version, replaying an identical import and refusing a conflicting one."""
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
    """Turn one validated case into an ORM row."""
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
    """Load one dataset by id, or by name and version, raising when it is missing."""
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
    """Return datasets, newest first."""
    if limit < 1:
        raise DatasetInvalid("limit must be at least 1")
    return session.scalars(select(EvalDataset).order_by(EvalDataset.id.desc()).limit(limit)).all()


def list_cases(
    session: Session,
    *,
    dataset_id: int,
    enabled_only: bool = False,
) -> Sequence[EvalCase]:
    """Return a dataset's cases in case-key order."""
    statement = select(EvalCase).where(EvalCase.dataset_id == dataset_id)
    if enabled_only:
        statement = statement.where(EvalCase.enabled.is_(True))
    return session.scalars(statement.order_by(EvalCase.case_key.asc())).all()
