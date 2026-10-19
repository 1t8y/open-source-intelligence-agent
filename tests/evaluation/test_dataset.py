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
from sqlalchemy import select
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
    DatasetTopicMissing,
    DatasetVersionConflict,
    EvaluationNotFound,
)
from open_source_intelligence.persistence.models.intelligence import Topic


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


def _import_dataset(session: Session, spec, **kwargs):
    """Import a dataset after making sure the topic it names exists.

    Since T25 `import_dataset` refuses an enabled case whose topic this deployment does not have,
    so the import-mechanics tests need a world that satisfies that precondition. Giving them one
    is not a workaround: production imports always happen in a deployment that has the topic.
    """
    workspace = Workspace(session)
    for name in sorted({case.topic for case in spec.cases if case.enabled}):
        if session.scalar(select(Topic).where(Topic.name == name)) is None:
            workspace.add_topic(name=name)
    return import_dataset(session, spec, **kwargs)


def test_import_creates_then_replays_an_identical_document(session: Session) -> None:
    """Re-importing the same content must be a no-op that reports `replayed`."""
    spec = parse_dataset(build_dataset_document())
    first = _import_dataset(session, spec)
    assert (first.created, first.replayed, first.case_count) == (True, False, 1)

    second = _import_dataset(session, parse_dataset(build_dataset_document()))
    assert (second.created, second.replayed) == (False, True)
    assert second.dataset_id == first.dataset_id
    assert len(list_cases(session, dataset_id=first.dataset_id)) == 1
    assert len(list_datasets(session)) == 1


def test_importing_a_changed_case_under_the_same_version_is_refused(session: Session) -> None:
    """Versions are immutable, so changed content must bump the version instead."""
    _import_dataset(session, parse_dataset(build_dataset_document()))
    changed = build_dataset_document(
        cases=[build_case_document(facts=[{"repo": "acme/rocket", "field": "stars", "value": 1}])]
    )
    with pytest.raises(DatasetVersionConflict) as excinfo:
        _import_dataset(session, parse_dataset(changed))
    assert "bump the version" in str(excinfo.value)


def test_a_new_version_creates_a_separate_dataset(session: Session) -> None:
    """Bumping the version must produce a second, independently addressable dataset."""
    first = _import_dataset(session, parse_dataset(build_dataset_document(version="v1")))
    second = _import_dataset(session, parse_dataset(build_dataset_document(version="v2")))
    assert first.dataset_id != second.dataset_id
    assert (second.created, second.replayed) == (True, False)
    assert len(list_datasets(session)) == 2


def test_the_reference_and_rubric_survive_a_round_trip(
    session: Session, workspace: Workspace
) -> None:
    """Guards against a driver-dependent JSON comparison breaking replay detection."""
    spec = load_dataset_file(DATASET_PATH)
    first = _import_dataset(session, spec)
    second = _import_dataset(session, load_dataset_file(DATASET_PATH))
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
    imported = _import_dataset(session, spec)
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
    imported = _import_dataset(session, parse_dataset(document))
    assert len(list_cases(session, dataset_id=imported.dataset_id)) == 2
    enabled = list_cases(session, dataset_id=imported.dataset_id, enabled_only=True)
    assert [row.case_key for row in enabled] == ["live"]


def test_import_records_the_actor(session: Session) -> None:
    """`created_by` must be persisted so a dataset has an accountable owner."""
    spec = parse_dataset(build_dataset_document())
    imported = _import_dataset(session, spec, created_by=None)
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


def test_import_refuses_an_enabled_case_whose_topic_is_missing(
    session: Session, workspace: Workspace
) -> None:
    """Importing a dataset bound to somebody else's topic must fail here, not at run time.

    The failure mode this prevents is expensive: every variant of every case runs, and each one
    comes back `failed` with `EVAL_TOPIC_NOT_FOUND`. Fifteen red rows read like a broken model,
    and the actual cause - a dataset written for another deployment - is one line in a JSON file.
    """
    spec = parse_dataset(build_dataset_document())

    with pytest.raises(DatasetTopicMissing) as excinfo:
        import_dataset(session, spec)

    assert GROWTH_TOPIC in str(excinfo.value)
    assert list_datasets(session) == []


def test_import_does_not_check_the_topic_of_a_disabled_case(
    session: Session, workspace: Workspace
) -> None:
    """A parked scenario is allowed to name a topic nobody has yet."""
    workspace.add_topic(name=GROWTH_TOPIC)
    document = build_dataset_document()
    document["cases"].append(
        build_case_document(case_key="parked", topic="a topic nobody created", enabled=False)
    )

    imported = _import_dataset(session, parse_dataset(document))

    assert imported.case_count == 2
    assert [row.case_key for row in list_cases(session, dataset_id=imported.dataset_id)] == [
        "case-a",
        "parked",
    ]


def test_replaying_a_version_does_not_recheck_topics(
    session: Session, workspace: Workspace
) -> None:
    """A replay is a content check, not a readiness check.

    Disabling a topic after its dataset was imported must not turn "nothing changed" into an
    error - otherwise a routine no-op import starts failing for a reason that has nothing to do
    with the file.
    """
    workspace.add_topic(name=GROWTH_TOPIC)
    spec = parse_dataset(build_dataset_document())
    first = _import_dataset(session, spec)
    for topic in session.scalars(select(Topic).where(Topic.name == GROWTH_TOPIC)):
        topic.enabled = False
    session.commit()

    replay = _import_dataset(session, spec)

    assert replay.dataset_id == first.dataset_id
    assert replay.replayed is True


def test_the_v2_dataset_binds_one_enabled_case_to_a_real_topic_name() -> None:
    """The shipped v2 file must be importable as written: one live case, two parked ones."""
    spec = load_dataset_file("datasets/weekly-agent-quality.v2.json")

    enabled = [case for case in spec.cases if case.enabled]
    parked = [case for case in spec.cases if not case.enabled]

    assert spec.version == "v2"
    assert [case.case_key for case in enabled] == ["growth-surge"]
    assert enabled[0].topic == "sweep-llm-agent-topic"
    assert {case.case_key for case in parked} == {"missing-readme", "injection-readme"}
    assert spec.cases[0].window_start.isoformat() == "2026-10-16T00:00:00+00:00"
