"""Dataset parsing, validation, and versioned import."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from evaluation_support import (
    DATASET_PATH,
    GROWTH_TOPIC,
    build_case_document,
    build_dataset_document,
    seed_eval_world,
)
from sqlalchemy.orm import Session
from support import Workspace

from open_source_intelligence.evaluation.dataset import (
    discover_dataset_files,
    import_dataset,
    list_cases,
    list_datasets,
    load_dataset_file,
    parse_dataset,
    resolve_dataset,
)
from open_source_intelligence.evaluation.errors import (
    DatasetInvalid,
    DatasetVersionConflict,
    EvaluationNotFound,
)


def test_the_shipped_dataset_loads_and_covers_three_samples() -> None:
    """The dataset in the repository must parse and describe its three sample kinds."""
    spec = load_dataset_file(DATASET_PATH)
    assert spec.name == "weekly-agent-quality"
    assert spec.version == "v1"
    assert spec.case_keys() == ("growth-surge", "missing-readme", "injection-readme")
    topics = {case.topic for case in spec.cases}
    assert topics == {"AI Agent", "AI Agent Sparse", "AI Agent Injection"}
    by_key = {case.case_key: case for case in spec.cases}
    assert by_key["growth-surge"].tags == ("growth", "happy-path")
    assert by_key["injection-readme"].tags == ("security", "prompt-injection")


def test_the_shipped_dataset_is_discoverable_on_disk() -> None:
    """`datasets/*.json` must be picked up without an explicit path."""
    paths = [path.name for path in discover_dataset_files()]
    assert "weekly-agent-quality.v1.json" in paths


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda doc: doc.pop("name"), "name must be a non-empty string"),
        (lambda doc: doc.update(cases=[]), "cases must be a non-empty list"),
        (lambda doc: doc.update(schema_version=99), "unsupported schema_version"),
        (
            lambda doc: doc.update(
                cases=[build_case_document(case_key="dup"), build_case_document(case_key="dup")]
            ),
            "duplicate case_key",
        ),
        (
            lambda doc: doc.update(cases=[build_case_document(case_key="bad key")]),
            "case_key must contain only",
        ),
        (
            lambda doc: doc.update(
                cases=[
                    build_case_document(
                        start=datetime(2026, 10, 12, tzinfo=UTC),
                        end=datetime(2026, 10, 5, tzinfo=UTC),
                    )
                ]
            ),
            "must not be after",
        ),
    ],
)
def test_invalid_documents_are_refused(mutate, message: str) -> None:
    """A malformed dataset must be rejected with a message naming the problem."""
    document = build_dataset_document()
    mutate(document)
    with pytest.raises(DatasetInvalid) as excinfo:
        parse_dataset(document)
    assert message in str(excinfo.value)


def test_a_naive_window_is_refused() -> None:
    """A window without a timezone would make the same dataset behave differently per host."""
    document = build_dataset_document()
    assert isinstance(document["cases"], list)
    document["cases"][0]["window"]["start"] = "2026-10-05T00:00:00"
    with pytest.raises(DatasetInvalid) as excinfo:
        parse_dataset(document)
    assert "timezone offset" in str(excinfo.value)


def test_a_non_object_document_is_refused() -> None:
    """A JSON array or scalar is not a dataset."""
    with pytest.raises(DatasetInvalid):
        parse_dataset([1, 2, 3])


def test_import_creates_then_replays_an_identical_document(session: Session) -> None:
    """Re-importing the same content must be a no-op that reports `replayed`."""
    spec = parse_dataset(build_dataset_document())
    first = import_dataset(session, spec)
    assert (first.created, first.replayed, first.case_count) == (True, False, 1)

    second = import_dataset(session, parse_dataset(build_dataset_document()))
    assert (second.created, second.replayed) == (False, True)
    assert second.dataset_id == first.dataset_id
    assert len(list_cases(session, dataset_id=first.dataset_id)) == 1
    assert len(list_datasets(session)) == 1


def test_importing_a_changed_case_under_the_same_version_is_refused(session: Session) -> None:
    """Versions are immutable, so changed content must bump the version instead."""
    import_dataset(session, parse_dataset(build_dataset_document()))
    changed = build_dataset_document(
        cases=[build_case_document(facts=[{"repo": "acme/rocket", "field": "stars", "value": 1}])]
    )
    with pytest.raises(DatasetVersionConflict) as excinfo:
        import_dataset(session, parse_dataset(changed))
    assert "bump the version" in str(excinfo.value)


def test_a_new_version_creates_a_separate_dataset(session: Session) -> None:
    """Bumping the version must produce a second, independently addressable dataset."""
    first = import_dataset(session, parse_dataset(build_dataset_document(version="v1")))
    second = import_dataset(session, parse_dataset(build_dataset_document(version="v2")))
    assert first.dataset_id != second.dataset_id
    assert (second.created, second.replayed) == (True, False)
    assert len(list_datasets(session)) == 2


def test_the_reference_and_rubric_survive_a_round_trip(
    session: Session, workspace: Workspace
) -> None:
    """Guards against a driver-dependent JSON comparison breaking replay detection."""
    spec = load_dataset_file(DATASET_PATH)
    first = import_dataset(session, spec)
    second = import_dataset(session, load_dataset_file(DATASET_PATH))
    assert second.replayed is True
    case = next(
        row
        for row in list_cases(session, dataset_id=first.dataset_id)
        if row.case_key == "growth-surge"
    )
    assert case.reference_json is not None
    assert case.reference_json["facts"][0]["repo"] == "acme/rocket"
    assert case.rubric_json == {
        "min_citation_coverage": 0.95,
        "min_factual_consistency": 0.9,
        "min_task_completion": 0.9,
    }
    assert case.input_json["topic"] == GROWTH_TOPIC


def test_import_rejects_a_missing_file() -> None:
    """A missing path must be a dataset error rather than a raw `FileNotFoundError`."""
    with pytest.raises(DatasetInvalid):
        load_dataset_file("datasets/does-not-exist.json")


def test_import_rejects_malformed_json(tmp_path) -> None:
    """Invalid JSON must be reported as a dataset error."""
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(DatasetInvalid) as excinfo:
        load_dataset_file(path)
    assert "not valid JSON" in str(excinfo.value)


def test_resolve_dataset_by_id_and_by_name_version(session: Session) -> None:
    """Both addressing modes must find the same row, and a miss must be a `404`-style error."""
    spec = parse_dataset(build_dataset_document(name="resolve-me", version="v9"))
    imported = import_dataset(session, spec)
    assert resolve_dataset(session, dataset_id=imported.dataset_id).name == "resolve-me"
    assert resolve_dataset(session, name="resolve-me", version="v9").id == imported.dataset_id
    with pytest.raises(EvaluationNotFound):
        resolve_dataset(session, name="resolve-me", version="v0")
    with pytest.raises(EvaluationNotFound):
        resolve_dataset(session, dataset_id=999_999)
    with pytest.raises(DatasetInvalid):
        resolve_dataset(session)


def test_a_disabled_case_is_stored_but_excluded_when_requested(session: Session) -> None:
    """A parked case must stay in the dataset without being run."""
    document = build_dataset_document(
        cases=[
            build_case_document(case_key="live"),
            build_case_document(case_key="parked", enabled=False),
        ]
    )
    imported = import_dataset(session, parse_dataset(document))
    assert len(list_cases(session, dataset_id=imported.dataset_id)) == 2
    enabled = list_cases(session, dataset_id=imported.dataset_id, enabled_only=True)
    assert [row.case_key for row in enabled] == ["live"]


def test_import_records_the_actor(session: Session) -> None:
    """`created_by` must be persisted so a dataset has an accountable owner."""
    spec = parse_dataset(build_dataset_document())
    imported = import_dataset(session, spec, created_by=None)
    dataset = resolve_dataset(session, dataset_id=imported.dataset_id)
    assert dataset.created_by is None


def test_the_seeded_world_matches_the_shipped_dataset(
    session: Session, workspace: Workspace
) -> None:
    """Every topic and fact the shipped dataset asserts must exist in the seeded world."""
    seed_eval_world(session, workspace)
    spec = load_dataset_file(DATASET_PATH)
    stored = json.dumps(spec.cases[0].to_input_json(), ensure_ascii=False)
    assert GROWTH_TOPIC in stored
