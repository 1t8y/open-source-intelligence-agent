"""Tests for the evaluation CLI.

`run_action(args, session)` is the seam: it takes an already-parsed namespace and a live session,
so every action can be exercised against the real dataset, runner, and persistence stack without
spawning an interpreter or opening a database engine.

`main()` is tested separately because it owns the two things a shell script depends on: one JSON
document on stdout, and a non-zero exit with a `code` on failure. A CLI that prints a traceback
instead is unusable from a pipeline, so that contract is pinned here rather than assumed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from evaluation_support import (
    DATASET_PATH,
    build_case_document,
    build_dataset_document,
    import_shipped_dataset,
    seed_eval_world,
)
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace, build_settings

from open_source_intelligence.evaluation import cli
from open_source_intelligence.evaluation.targets import BASELINE_TARGET, TARGET_ORDER


def _args(**overrides: object) -> argparse.Namespace:
    """Build a full namespace, since `parse_args` always populates every field."""
    values: dict[str, object] = {
        "action": "list",
        "dataset_file": None,
        "dataset_dir": "datasets",
        "dataset_id": None,
        "name": None,
        "version": None,
        "targets": None,
        "limit": 50,
        "actor_id": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class _Engine:
    """Stand-in for a SQLAlchemy engine so `main()` can dispose something harmless."""

    def dispose(self) -> None:
        """Accept the call and do nothing."""


@pytest.fixture()
def live(session: Session, workspace: Workspace) -> int:
    """Seed the world and import the shipped dataset for the CLI to act on."""
    seed_eval_world(session, workspace)
    return import_shipped_dataset(session)


@pytest.fixture()
def wired(monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker[Session]) -> None:
    """Point `main()` at the shared test database and a deterministic settings object."""
    monkeypatch.setattr(cli, "get_settings", lambda: build_settings())
    monkeypatch.setattr(cli, "create_database_engine", lambda url: _Engine())
    monkeypatch.setattr(cli, "create_session_factory", lambda engine: session_factory)


def _captured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str
) -> object:
    """Run `main()` and decode the single JSON document on stdout."""
    monkeypatch.setattr(cli.sys, "argv", ["eval-cli", *argv])
    cli.main()
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# --------------------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------------------


def test_list_is_empty_before_anything_is_imported(session: Session) -> None:
    """An operator's first command must work on an empty database."""
    payload = cli.run_action(_args(action="list"), session)

    assert payload == {"items": []}


def test_list_reports_each_dataset_with_its_case_count(session: Session, live: int) -> None:
    """The listing is how an operator finds the id they need for `run`."""
    payload = cli.run_action(_args(action="list"), session)

    items = payload["items"]
    assert [item["dataset_id"] for item in items] == [live]
    assert items[0]["name"] == "weekly-agent-quality"
    assert items[0]["version"] == "v1"
    assert items[0]["case_count"] == 3


def test_list_refuses_a_non_positive_limit(session: Session) -> None:
    """A zero limit would silently look like an empty corpus."""
    with pytest.raises(SystemExit) as excinfo:
        cli.run_action(_args(action="list", limit=0), session)
    assert "limit" in str(excinfo.value)


# --------------------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------------------


def test_import_from_a_single_file_records_a_new_version(
    session: Session, workspace: Workspace
) -> None:
    """`--dataset-file` is the explicit path an operator uses for a new dataset version."""
    seed_eval_world(session, workspace)

    payload = cli.run_action(
        _args(action="import", dataset_file=DATASET_PATH, actor_id=7),
        session,
    )

    imported = payload["imported"]
    assert len(imported) == 1
    assert imported[0]["name"] == "weekly-agent-quality"
    assert imported[0]["created"] is True
    assert imported[0]["replayed"] is False
    assert imported[0]["case_count"] == 3


def test_reimporting_the_same_file_is_a_replay(session: Session, workspace: Workspace) -> None:
    """Importing twice must be safe, which is what makes the command idempotent in CI."""
    seed_eval_world(session, workspace)
    args = _args(action="import", dataset_file=DATASET_PATH)

    first = cli.run_action(args, session)["imported"][0]
    second = cli.run_action(args, session)["imported"][0]

    assert first["created"] is True
    assert second["created"] is False
    assert second["replayed"] is True
    assert second["dataset_id"] == first["dataset_id"]


def test_import_walks_the_dataset_directory(session: Session, workspace: Workspace) -> None:
    """With no `--dataset-file`, every dataset the repository ships is imported."""
    seed_eval_world(session, workspace)

    payload = cli.run_action(_args(action="import", dataset_dir="datasets"), session)

    names = sorted(item["name"] for item in payload["imported"])
    assert names == ["weekly-agent-quality"]


def test_import_from_an_empty_directory_exits(session: Session, tmp_path: Path) -> None:
    """Silently importing nothing would look like success."""
    with pytest.raises(SystemExit) as excinfo:
        cli.run_action(_args(action="import", dataset_dir=str(tmp_path)), session)
    assert "no dataset file" in str(excinfo.value)


# --------------------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------------------


def test_run_by_id_produces_the_unified_report(session: Session, live: int) -> None:
    """The headline CLI action: one command, every variant, one comparable report."""
    payload = cli.run_action(_args(action="run", dataset_id=live), session)

    assert payload["dataset"]["id"] == live
    assert payload["case_count"] == 3
    assert [item["target_type"] for item in payload["targets"]] == list(TARGET_ORDER)
    assert payload["passed"] is True


def test_run_accepts_a_repeated_target_flag(session: Session, live: int) -> None:
    """`--target` is repeatable so a focused sweep does not need five invocations."""
    payload = cli.run_action(
        _args(action="run", dataset_id=live, targets=["agent_full", BASELINE_TARGET]),
        session,
    )

    assert [item["target_type"] for item in payload["targets"]] == [
        BASELINE_TARGET,
        "agent_full",
    ]


def test_run_resolves_the_dataset_by_name_and_version(session: Session, live: int) -> None:
    """An operator should not have to look up an id to re-run a known version."""
    payload = cli.run_action(
        _args(
            action="run",
            name="weekly-agent-quality",
            version="v1",
            targets=[BASELINE_TARGET],
        ),
        session,
    )

    assert payload["dataset"]["id"] == live
    assert payload["dataset"]["version"] == "v1"


def test_run_reports_an_unknown_target_as_a_domain_error(session: Session, live: int) -> None:
    """The stable code is what a script branches on, so it must survive to the caller."""
    from open_source_intelligence.evaluation.errors import UnknownTarget

    with pytest.raises(UnknownTarget) as excinfo:
        cli.run_action(_args(action="run", dataset_id=live, targets=["agent_oops"]), session)
    assert excinfo.value.code == "UNKNOWN_TARGET"


# --------------------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------------------


def test_compare_reproduces_the_last_report(session: Session, live: int) -> None:
    """`compare` must not execute anything; it reads what the last sweep stored."""
    ran = cli.run_action(_args(action="run", dataset_id=live), session)

    payload = cli.run_action(_args(action="compare", dataset_id=live), session)

    assert [item["target_type"] for item in payload["targets"]] == [
        item["target_type"] for item in ran["targets"]
    ]
    assert payload["passed"] == ran["passed"]


def test_compare_requires_a_dataset_id(session: Session) -> None:
    """Without an id there is nothing to compare, and guessing would be worse than failing."""
    with pytest.raises(SystemExit) as excinfo:
        cli.run_action(_args(action="compare"), session)
    assert "--dataset-id" in str(excinfo.value)


def test_compare_refuses_a_dataset_with_no_run(session: Session, live: int) -> None:
    """An empty comparison would read as a pass."""
    from open_source_intelligence.evaluation.errors import EvaluationNotFound

    with pytest.raises(EvaluationNotFound):
        cli.run_action(_args(action="compare", dataset_id=live), session)


# --------------------------------------------------------------------------------------
# main(): the process contract
# --------------------------------------------------------------------------------------


def test_main_prints_one_json_document(
    wired: None,
    live: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A script parses stdout; anything else on that stream would break it."""
    payload = _captured(monkeypatch, capsys, "--action", "list")

    assert isinstance(payload, dict)
    assert [item["dataset_id"] for item in payload["items"]] == [live]


def test_main_exits_non_zero_with_a_code_on_failure(
    wired: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A domain failure must be a machine-readable document, not a traceback."""
    monkeypatch.setattr(
        cli.sys, "argv", ["eval-cli", "--action", "compare", "--dataset-id", "987654"]
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    body = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert body["action"] == "compare"
    assert body["code"] == "NOT_FOUND"


def test_main_keeps_the_action_in_the_error_document(
    wired: None,
    live: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Which action failed is part of the diagnosis, so it travels with the code."""
    monkeypatch.setattr(
        cli.sys,
        "argv",
        ["eval-cli", "--action", "run", "--dataset-id", str(live), "--target", "agent_oops"],
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    body = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert body["action"] == "run"
    assert body["code"] == "UNKNOWN_TARGET"


def test_a_hand_written_dataset_imports_and_runs(
    session: Session,
    workspace: Workspace,
    tmp_path: Path,
) -> None:
    """The documented authoring loop: write JSON, import it, run it, read the report."""
    seed_eval_world(session, workspace)
    document = build_dataset_document(
        name="cli-authored",
        cases=[build_case_document(case_key="only-case")],
    )
    target = tmp_path / "cli-authored.v1.json"
    target.write_text(json.dumps(document), encoding="utf-8")

    imported = cli.run_action(_args(action="import", dataset_file=str(target)), session)
    dataset_id = imported["imported"][0]["dataset_id"]
    report = cli.run_action(
        _args(action="run", dataset_id=dataset_id, targets=[BASELINE_TARGET]),
        session,
    )

    assert imported["imported"][0]["created"] is True
    assert report["case_count"] == 1
    assert [item["target_type"] for item in report["targets"]] == [BASELINE_TARGET]
    assert report["targets"][0]["reference_target"] is None


def test_the_cli_output_survives_json_dumps(session: Session, live: int) -> None:
    """`Decimal` rates and dates must already be strings by the time the CLI serializes."""
    for args in (
        _args(action="list"),
        _args(action="run", dataset_id=live, targets=[BASELINE_TARGET]),
        _args(action="compare", dataset_id=live),
    ):
        payload = cli.run_action(args, session)
        assert json.loads(json.dumps(payload)) == payload
