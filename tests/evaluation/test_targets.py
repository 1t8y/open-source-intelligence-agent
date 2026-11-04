"""用真实的研究图跑一遍五个评估目标。

这些刻意做成集成用例：消融设计的全部主张就是「一个变体跑*生产*流水线，
只关掉一项能力」，所以把图打桩掉的用例什么都验证不了。
"""

from __future__ import annotations

from evaluation_support import (
    INJECTION_TOPIC,
    WINDOW_END,
    WINDOW_START,
    import_shipped_dataset,
    seed_eval_world,
)
from sqlalchemy.orm import Session
from support import Workspace, build_settings

from open_source_intelligence.agent.profile import (
    AGENT_FULL,
    PROFILE_NO_HISTORY,
    PROFILE_NO_TOOLS,
    PROFILE_NO_VERIFIER,
    READ_TOOLS,
    WRITE_TOOL,
    profile_for_target,
)
from open_source_intelligence.agent.runtime import apply_profile, resolve_allowed_tools
from open_source_intelligence.agent.tools.registry import WHITELIST
from open_source_intelligence.evaluation.targets import (
    BASELINE_TARGET,
    ERROR_TOPIC_NOT_FOUND,
    run_target,
)
from open_source_intelligence.persistence.models.evaluation import EvalCase
from open_source_intelligence.persistence.models.intelligence import Topic

SETTINGS = build_settings()

AGENT_TARGETS = ("agent_full", "agent_no_verifier", "agent_no_tools", "agent_no_history")


def _case(session: Session, case_key: str) -> EvalCase:
    """从随附数据集里返回一个用例行。"""
    dataset_id = import_shipped_dataset(session)
    from open_source_intelligence.evaluation.dataset import list_cases

    rows = list_cases(session, dataset_id=dataset_id)
    return next(row for row in rows if row.case_key == case_key)


def test_the_full_agent_produces_a_fully_cited_draft(
    session: Session, workspace: Workspace
) -> None:
    """证明生产档位能产出一份完整、有证据支撑的草稿。"""
    seed_eval_world(session, workspace)
    case = _case(session, "growth-surge")
    output = run_target(
        session,
        case=case,
        target_type="agent_full",
        settings=SETTINGS,
        now=WINDOW_END,
    )
    assert output.succeeded, output.error_message
    assert len(output.sections) == 6
    assert all(section.citations for section in output.sections)
    assert output.verification_sufficient is True
    assert output.tool_call_total > 0
    assert output.tool_call_denied == 0
    assert output.guard_triggered is False
    assert output.agent_run_id is not None


def test_the_prior_report_is_actually_visible_to_the_agent(
    session: Session, workspace: Workspace
) -> None:
    """数据集承诺这个主题有已发布的历史，所以工具必须能把它取回来。

    `search_prior_reports` 按 `generated_at < before` 筛选，而 `before`
    是用例窗口的结束时刻。因此一个把上期报告精确打在窗口结束时刻的世界，
    看起来灌好了数据，实际却看不见；唯一征兆是草稿薄了一点 ——
    而这恰恰是那种把质量指标变成噪声的夹具漂移。
    """
    seed_eval_world(session, workspace)
    case = _case(session, "growth-surge")
    output = run_target(
        session,
        case=case,
        target_type="agent_full",
        settings=SETTINGS,
        now=WINDOW_END,
    )
    source_types = {
        citation.source_type for section in output.sections for citation in section.citations
    }
    assert "prior_report" in source_types
    assert "metric" in source_types


def test_the_baseline_runs_without_tools_or_citations(
    session: Session, workspace: Workspace
) -> None:
    """单次提示没有任何工具观测，而这正是它无从引用的东西。"""
    seed_eval_world(session, workspace)
    case = _case(session, "growth-surge")
    output = run_target(session, case=case, target_type=BASELINE_TARGET, settings=SETTINGS)
    assert output.succeeded, output.error_message
    assert len(output.sections) == 6
    assert output.tool_call_total == 0
    assert all(not section.citations for section in output.sections)
    assert output.verification_sufficient is None
    assert output.agent_run_id is None
    assert output.notes


def test_the_injection_sample_is_caught_by_the_prompt_guard(
    session: Session, workspace: Workspace
) -> None:
    """投毒的 README 文本必须点亮守卫标志，而不是作为指令抵达模型。"""
    seed_eval_world(session, workspace)
    case = _case(session, "injection-readme")
    output = run_target(
        session,
        case=case,
        target_type="agent_full",
        settings=SETTINGS,
        now=WINDOW_END,
    )
    assert output.tool_call_total > 0
    assert output.guard_triggered is True
    # 守卫尽了职，所以这次运行是可打分的，尽管它还需要人来过目。
    assert output.sections


def test_no_verifier_removes_the_coverage_gate(session: Session, workspace: Workspace) -> None:
    """证明 `agent_no_verifier` 给出的结论比完整运行更宽松。"""
    seed_eval_world(session, workspace)
    case = _case(session, "injection-readme")
    full = run_target(
        session, case=case, target_type="agent_full", settings=SETTINGS, now=WINDOW_END
    )
    ablated = run_target(
        session,
        case=case,
        target_type="agent_no_verifier",
        settings=SETTINGS,
        now=WINDOW_END,
    )
    # 注入用例正是完整校验器拒绝放行的那个，所以消融带来的差别看得见，
    # 而不会被一份本就充分的证据底座盖住。
    assert full.verification_sufficient is False
    assert ablated.verification_sufficient is True
    assert not ablated.notes or "verification" not in ablated.notes[0]


def test_no_tools_collapses_citation_support(session: Session, workspace: Workspace) -> None:
    """没有读取工具，草稿就没有观测，因而也不带引用。"""
    from sqlalchemy import select

    from open_source_intelligence.persistence.models.agent import ToolCall

    seed_eval_world(session, workspace)
    case = _case(session, "growth-surge")
    full = run_target(
        session, case=case, target_type="agent_full", settings=SETTINGS, now=WINDOW_END
    )
    ablated = run_target(
        session,
        case=case,
        target_type="agent_no_tools",
        settings=SETTINGS,
        now=WINDOW_END,
    )
    assert full.tool_call_read > 0
    # 消融版仍然写出六个章节，所以有意义的判断是没有任何*读取*工具跑过：
    # `save_report_section` 不算采集证据，也不能被算作读取。
    assert ablated.tool_call_read == 0
    assert ablated.tool_call_total == len(ablated.sections)
    read_rows = session.scalars(
        select(ToolCall.tool_name).where(
            ToolCall.agent_run_id == ablated.agent_run_id,
            ToolCall.tool_name != "save_report_section",
        )
    ).all()
    assert read_rows == []
    assert all(not section.citations for section in ablated.sections)


def test_no_history_drops_the_prior_report_baseline(session: Session, workspace: Workspace) -> None:
    """去掉 `search_prior_reports` 之后，运行就没有可比的基线。"""
    seed_eval_world(session, workspace)
    case = _case(session, "growth-surge")
    full = run_target(
        session, case=case, target_type="agent_full", settings=SETTINGS, now=WINDOW_END
    )
    ablated = run_target(
        session,
        case=case,
        target_type="agent_no_history",
        settings=SETTINGS,
        now=WINDOW_END,
    )
    assert full.verification_sufficient is True
    assert ablated.verification_sufficient is False
    assert ablated.notes and "verification insufficient" in ablated.notes[0]
    # 工具是被拒绝，而不是被悄悄跳过，审计轨迹把它记成了 denied。
    assert ablated.tool_call_denied == 1


def test_every_agent_variant_still_writes_a_draft(session: Session, workspace: Workspace) -> None:
    """产不出草稿的消融什么都测不到，所以四个变体都必须写出草稿。"""
    seed_eval_world(session, workspace)
    for target_type in AGENT_TARGETS:
        case = _case(session, "growth-surge")
        output = run_target(
            session,
            case=case,
            target_type=target_type,
            settings=SETTINGS,
            now=WINDOW_END,
        )
        assert output.succeeded, f"{target_type}: {output.error_message}"
        assert len(output.sections) == 6, target_type


def test_a_missing_topic_fails_the_case_instead_of_raising(
    session: Session, workspace: Workspace
) -> None:
    """主题缺席的用例必须被记成失败，而不是把整轮跑崩。"""
    seed_eval_world(session, workspace)
    case = _case(session, "growth-surge")
    topic = session.query(Topic).filter(Topic.name == "AI Agent").one()
    topic.enabled = False
    session.commit()
    output = run_target(
        session,
        case=case,
        target_type="agent_full",
        settings=SETTINGS,
        now=WINDOW_END,
    )
    assert output.status == "failed"
    assert output.error_code == ERROR_TOPIC_NOT_FOUND


def test_case_context_carries_the_window_from_the_document(
    session: Session, workspace: Workspace
) -> None:
    """落库的窗口必须一路带进运行，不能回退到主题的默认值。"""
    seed_eval_world(session, workspace)
    case = _case(session, "growth-surge")
    assert case.input_json["window"]["start"] == WINDOW_START.isoformat()
    assert case.input_json["window"]["end"] == WINDOW_END.isoformat()


def test_unknown_target_is_refused(session: Session, workspace: Workspace) -> None:
    """写错的名字不能悄悄回退到别的变体。"""
    seed_eval_world(session, workspace)
    case = _case(session, "growth-surge")
    try:
        run_target(session, case=case, target_type="agent_typo", settings=SETTINGS)
    except ValueError as exc:
        assert "unsupported eval target" in str(exc)
    else:  # pragma: no cover - 抛出异常本身就是断言
        raise AssertionError("an unknown target must raise")


def test_profiles_narrow_but_never_remove_the_write_tool() -> None:
    """档位只能削掉能力，永远不能删掉产出草稿的那个工具。"""
    configured = resolve_allowed_tools({"tools": list(WHITELIST)})
    assert apply_profile(configured, AGENT_FULL) == configured

    no_history = apply_profile(configured, PROFILE_NO_HISTORY)
    assert "search_prior_reports" not in no_history
    assert WRITE_TOOL in no_history
    assert set(no_history) == set(configured) - {"search_prior_reports"}

    no_tools = apply_profile(configured, PROFILE_NO_TOOLS)
    assert set(no_tools) == {WRITE_TOOL}
    for read_tool in READ_TOOLS:
        assert read_tool not in no_tools


def test_a_config_that_never_granted_the_write_tool_does_not_regain_it() -> None:
    """档位不能把 Agent 配置刻意扣住的权限放宽。"""
    configured = resolve_allowed_tools({"tools": ["query_repo_metrics"]})
    narrowed = apply_profile(configured, PROFILE_NO_TOOLS)
    assert WRITE_TOOL not in narrowed
    assert narrowed == ()


def test_profile_lookup_matches_the_documented_variants() -> None:
    """数据集里的变体名必须映射到执行器预期的档位。"""
    assert profile_for_target("agent_full") is AGENT_FULL
    assert profile_for_target("agent_no_verifier") is PROFILE_NO_VERIFIER
    assert profile_for_target("agent_no_tools") is PROFILE_NO_TOOLS
    assert profile_for_target("agent_no_history") is PROFILE_NO_HISTORY
    assert profile_for_target(BASELINE_TARGET) is None


def test_the_injection_topic_is_isolated_from_the_others(
    session: Session, workspace: Workspace
) -> None:
    """只有注入样本能读到投毒文本，否则每个用例都会报一次守卫命中。"""
    seed_eval_world(session, workspace)
    clean = run_target(
        session,
        case=_case(session, "growth-surge"),
        target_type="agent_full",
        settings=SETTINGS,
        now=WINDOW_END,
    )
    assert clean.guard_triggered is False
    assert INJECTION_TOPIC != "AI Agent"
