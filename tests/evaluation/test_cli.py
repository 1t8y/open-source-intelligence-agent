"""守护评估 CLI。

`run_action(args, session)` 是那条接缝：它接一个已解析好的命名空间
和一个活动会话，于是每个动作都能对着真实的数据集、执行器与持久化栈跑一遍，
既不用起解释器，也不用打开数据库引擎。

`main()` 单独测，因为它负责两件 shell 脚本依赖的事：stdout 上
只有一份 JSON 文档，
失败时带着 `code` 以非零码退出。只会打一坨 traceback 的 CLI
在流水线里根本没法用，所以这条契约要钉住，而不是想当然。
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
    """构造一个字段齐全的命名空间，因为 `parse_args` 总会填满每个字段。"""
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
    """SQLAlchemy 引擎的替身，好让 `main()` 能 dispose 一个无害的东西。"""

    def dispose(self) -> None:
        """接受调用，什么也不做。"""


@pytest.fixture()
def live(session: Session, workspace: Workspace) -> int:
    """灌好世界并导入随附数据集，供 CLI 操作。"""
    seed_eval_world(session, workspace)
    return import_shipped_dataset(session)


@pytest.fixture()
def wired(monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker[Session]) -> None:
    """把 `main()` 指向共享测试数据库与一套确定的配置。"""
    monkeypatch.setattr(cli, "get_settings", lambda: build_settings())
    monkeypatch.setattr(cli, "create_database_engine", lambda url: _Engine())
    monkeypatch.setattr(cli, "create_session_factory", lambda engine: session_factory)


def _captured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str
) -> object:
    """跑一遍 `main()`，并解出 stdout 上那唯一一份 JSON 文档。"""
    monkeypatch.setattr(cli.sys, "argv", ["eval-cli", *argv])
    cli.main()
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# --------------------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------------------


def test_list_is_empty_before_anything_is_imported(session: Session) -> None:
    """运维敲的第一条命令必须在空库上也能用。"""
    payload = cli.run_action(_args(action="list"), session)

    assert payload == {"items": []}


def test_list_reports_each_dataset_with_its_case_count(session: Session, live: int) -> None:
    """列出来，运维才知道 `run` 要用的那个 id。"""
    payload = cli.run_action(_args(action="list"), session)

    items = payload["items"]
    assert [item["dataset_id"] for item in items] == [live]
    assert items[0]["name"] == "weekly-agent-quality"
    assert items[0]["version"] == "v1"
    assert items[0]["case_count"] == 3


def test_list_refuses_a_non_positive_limit(session: Session) -> None:
    """上限为零会被误读成语料是空的。"""
    with pytest.raises(SystemExit) as excinfo:
        cli.run_action(_args(action="list", limit=0), session)
    assert "limit" in str(excinfo.value)


# --------------------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------------------


def test_import_from_a_single_file_records_a_new_version(
    session: Session, workspace: Workspace
) -> None:
    """`--dataset-file` 是运维为新数据版本指定显式路径时用的。"""
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
    """重复导入必须安全，这条命令在 CI 里才是幂等的。"""
    seed_eval_world(session, workspace)
    args = _args(action="import", dataset_file=DATASET_PATH)

    first = cli.run_action(args, session)["imported"][0]
    second = cli.run_action(args, session)["imported"][0]

    assert first["created"] is True
    assert second["created"] is False
    assert second["replayed"] is True
    assert second["dataset_id"] == first["dataset_id"]


def test_import_walks_the_dataset_directory(session: Session, workspace: Workspace) -> None:
    """不给 `--dataset-file` 时，仓库随附的每份数据集都会被导入。"""
    seed_eval_world(session, workspace)

    payload = cli.run_action(_args(action="import", dataset_dir="datasets"), session)

    names = sorted(item["name"] for item in payload["imported"])
    assert names == ["weekly-agent-quality"]


def test_import_from_an_empty_directory_exits(session: Session, tmp_path: Path) -> None:
    """静悄悄什么都没导入，会被当成成功。"""
    with pytest.raises(SystemExit) as excinfo:
        cli.run_action(_args(action="import", dataset_dir=str(tmp_path)), session)
    assert "no dataset file" in str(excinfo.value)


# --------------------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------------------


def test_run_by_id_produces_the_unified_report(session: Session, live: int) -> None:
    """CLI 的核心动作：一条命令、全部变体、一份可比的报告。"""
    payload = cli.run_action(_args(action="run", dataset_id=live), session)

    assert payload["dataset"]["id"] == live
    assert payload["case_count"] == 3
    assert [item["target_type"] for item in payload["targets"]] == list(TARGET_ORDER)
    assert payload["passed"] is True


def test_run_accepts_a_repeated_target_flag(session: Session, live: int) -> None:
    """`--target` 可以重复，这样一次聚焦的扫描不必分五次调用。"""
    payload = cli.run_action(
        _args(action="run", dataset_id=live, targets=["agent_full", BASELINE_TARGET]),
        session,
    )

    assert [item["target_type"] for item in payload["targets"]] == [
        BASELINE_TARGET,
        "agent_full",
    ]


def test_run_resolves_the_dataset_by_name_and_version(session: Session, live: int) -> None:
    """重跑一个已知版本时，运维不该还得先查出 id。"""
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
    """脚本按稳定的 code 分支，所以它必须一路传到调用方。"""
    from open_source_intelligence.evaluation.errors import UnknownTarget

    with pytest.raises(UnknownTarget) as excinfo:
        cli.run_action(_args(action="run", dataset_id=live, targets=["agent_oops"]), session)
    assert excinfo.value.code == "UNKNOWN_TARGET"


# --------------------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------------------


def test_compare_reproduces_the_last_report(session: Session, live: int) -> None:
    """`compare` 什么都不能执行；它读的是上一轮扫描存下的东西。"""
    ran = cli.run_action(_args(action="run", dataset_id=live), session)

    payload = cli.run_action(_args(action="compare", dataset_id=live), session)

    assert [item["target_type"] for item in payload["targets"]] == [
        item["target_type"] for item in ran["targets"]
    ]
    assert payload["passed"] == ran["passed"]


def test_compare_requires_a_dataset_id(session: Session) -> None:
    """没有 id 就没有可比的东西，猜一个比直接失败更糟。"""
    with pytest.raises(SystemExit) as excinfo:
        cli.run_action(_args(action="compare"), session)
    assert "--dataset-id" in str(excinfo.value)


def test_compare_refuses_a_dataset_with_no_run(session: Session, live: int) -> None:
    """空对比读起来会像一次通过。"""
    from open_source_intelligence.evaluation.errors import EvaluationNotFound

    with pytest.raises(EvaluationNotFound):
        cli.run_action(_args(action="compare", dataset_id=live), session)


# --------------------------------------------------------------------------------------
# main()：进程层面的契约
# --------------------------------------------------------------------------------------


def test_main_prints_one_json_document(
    wired: None,
    live: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """脚本要解析 stdout，这条流上多出任何东西都会把它弄坏。"""
    payload = _captured(monkeypatch, capsys, "--action", "list")

    assert isinstance(payload, dict)
    assert [item["dataset_id"] for item in payload["items"]] == [live]


def test_main_exits_non_zero_with_a_code_on_failure(
    wired: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """领域失败必须产出机器可读的文档，而不是 traceback。"""
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
    """是哪个动作失败了，本身就是诊断的一部分，所以它跟着 code 一起走。"""
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
    """文档写明的创作回路：写 JSON、导入、运行、读报告。"""
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
    """CLI 序列化之前，`Decimal` 比率与日期就必须已经是字符串。"""
    for args in (
        _args(action="list"),
        _args(action="run", dataset_id=live, targets=[BASELINE_TARGET]),
        _args(action="compare", dataset_id=live),
    ):
        payload = cli.run_action(args, session)
        assert json.loads(json.dumps(payload)) == payload
