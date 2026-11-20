"""守护 `--action finish`：把「生成 → 验证 → 发布策略」收成一条命令。

这条动作的存在理由是一次真实的踩空：报告在库里，验证是 `passed`，邮件也发出去了，而
平台上那条报告的 `publish_at` 一直是 NULL —— 因为 `generate` 只走到报告行，发布策略压
根没有被评估过。手工三步里漏掉最后一步，报告就安静地停在 `pending_review`，到期的清扫
只扫 `approved`/`scheduled`，谁也不会再来推进它。

所以这里钉的是两件事：`finish` 真的复用了自动成稿那条边（不是另写一条更容易走偏的
路径），以及草稿编排失败时退出码是 1 —— 验证不过不算失败，报告已经在库里等人评审。
"""

from __future__ import annotations

import argparse
import sys

import pytest
from agent_support import seed_topic
from sqlalchemy.orm import Session, sessionmaker
from support import NOW, Workspace, build_settings

from open_source_intelligence.agent.runtime import run_research_agent
from open_source_intelligence.persistence.models.reporting import Report
from open_source_intelligence.reports import cli


class _DisposableEngine:
    """替身引擎：`main()` 只用到 `dispose()`。"""

    disposed = False

    def dispose(self) -> None:
        """记录释放动作，便于确认 `main()` 走到了收尾。"""
        _DisposableEngine.disposed = True


def _parse(monkeypatch: pytest.MonkeyPatch, *argv: str) -> argparse.Namespace:
    """以给定命令行参数解析一次，避免触碰进程真实的 `sys.argv`。"""
    monkeypatch.setattr(sys, "argv", ["open_source_intelligence.reports.cli", *argv])
    return cli.parse_args()


def test_finish_is_an_accepted_action(monkeypatch: pytest.MonkeyPatch) -> None:
    """`finish` 必须是一个合法的 `--action`；拼错的动作名只会被 argparse 拒绝。"""
    args = _parse(monkeypatch, "--action", "finish", "--agent-run-id", "7")

    assert args.action == "finish"


def test_generate_stays_the_default_action(monkeypatch: pytest.MonkeyPatch) -> None:
    """只给 `--agent-run-id` 仍然等价于 `generate`，T10 的调用方不需要改。"""
    assert _parse(monkeypatch, "--agent-run-id", "7").action == "generate"


def test_finish_requires_an_agent_run_id(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有目标运行就没有可推进的东西，缺参数时直接以可读提示退出。"""
    args = _parse(monkeypatch, "--action", "finish")

    with pytest.raises(SystemExit):
        cli.run_action(args, session)


def test_finish_walks_a_run_to_a_report(
    session: Session,
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一次研究运行经 `finish` 之后应当已经有报告，而不只是一份草稿。"""
    seeded = seed_topic(workspace, repo_count=2)
    result = run_research_agent(
        session,
        topic_id=seeded.topic.id,
        settings=build_settings(),
        now=NOW,
    )
    assert result.report_draft_id is not None

    args = _parse(monkeypatch, "--action", "finish", "--agent-run-id", str(result.agent_run_id))
    payload = cli.run_action(args, session)

    assert payload["stage"] == "publish"
    assert payload["verdict"] == "passed"
    report = session.get(Report, payload["report_id"])
    assert report is not None
    # 落到哪个状态由策略决定；这里只在乎它靠自己离开了 `draft`。
    assert report.status in {"scheduled", "published", "pending_review", "approved"}


def test_main_exits_non_zero_only_when_the_draft_cannot_be_composed(
    session: Session,
    workspace: Workspace,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """没有候选 → 没有草稿 → 退出码 1，且失败原因在 JSON 里（脚本按 `code` 分支）。"""
    topic = workspace.add_topic()
    result = run_research_agent(
        session,
        topic_id=topic.id,
        settings=build_settings(),
        now=NOW,
    )
    assert result.report_draft_id is None
    session.commit()

    _DisposableEngine.disposed = False
    monkeypatch.setattr(cli, "get_settings", build_settings)
    monkeypatch.setattr(cli, "create_database_engine", lambda url: _DisposableEngine())
    monkeypatch.setattr(cli, "create_session_factory", lambda engine: session_factory)
    _parse(monkeypatch, "--action", "finish", "--agent-run-id", str(result.agent_run_id))

    with pytest.raises(SystemExit) as caught:
        cli.main()

    assert caught.value.code == 1
    assert _DisposableEngine.disposed is True
    printed = capsys.readouterr().out
    assert '"stage": "generate"' in printed
    assert '"error_code":' in printed
