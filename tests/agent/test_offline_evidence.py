"""守护「从记录下来的工具观测重建证据」的用例。

它们覆盖的缺陷对仓库里所有类型检查都不可见：失败的工具调用与成功的调用都是 JSON
对象，`isinstance(payload, dict)` 两者都收。于是一次回答 `TOOL_NOT_FOUND` 的调用被
记成「缓存 README 已读过」，草稿引用了一份模型从未收到的文档，报告生成随后死在一个
源行根本不存在的引用上。下面每一条断言「失败被排除」的用例，都配了一条断言「成功
仍被吸收」的用例 —— 否则「什么都没收集」也能满足所谓的修复。
"""

import json
from typing import Any

from open_source_intelligence.agent.llm.messages import (
    ChatMessage,
    ModelToolCall,
    is_tool_error_payload,
    tool_error_payload,
)
from open_source_intelligence.agent.llm.offline import _collect_evidence, compose_sections

REPO_ID = 99


def tool_call(call_id: str, name: str, repo_id: int = REPO_ID) -> ChatMessage:
    """构造请求某个仓库工具的 assistant 轮次。"""
    return ChatMessage(
        role="assistant",
        tool_calls=(ModelToolCall(call_id=call_id, name=name, arguments={"repo_id": repo_id}),),
    )


def observation(call_id: str, name: str, payload: Any) -> ChatMessage:
    """构造图本该为那次调用追加的 tool 消息。"""
    return ChatMessage(
        role="tool",
        name=name,
        tool_call_id=call_id,
        content=json.dumps(payload, ensure_ascii=False),
    )


def failure(code: str = "TOOL_NOT_FOUND") -> dict[str, Any]:
    """构造图渲染出来的失败外壳。"""
    return tool_error_payload(code, "repository content not available")


def content_payload() -> dict[str, Any]:
    """构造一份成功的 `get_repository_content` 载荷。"""
    return {
        "repo_id": REPO_ID,
        "source_url": "https://github.com/example/repo/blob/main/README.md",
        "content": "# Rocket",
        "content_hash": "0" * 64,
        "truncated": False,
        "untrusted": True,
    }


def profile_payload() -> dict[str, Any]:
    """构造一份成功的 `get_repository_profile` 载荷。"""
    return {"full_name": "example/repo", "stars": 1234}


def readme_source_refs(messages: list[ChatMessage]) -> set[str]:
    """返回草稿最终会引用到的 readme 引用集合。

    之所以用集合，是因为同一条引用合法地会出现在不止一个章节里（读到 README 时，
    「风险」与「附录」都会引用缓存的那份）；这里在乎的只是这份文档到底有没有被引用。
    """
    sections = compose_sections(_collect_evidence(messages))
    return {
        str(citation["source_ref"])
        for section in sections
        for citation in section["citations"]
        if citation["source_type"] == "readme"
    }


def test_a_failed_read_is_not_filed_as_evidence() -> None:
    """证明 `TOOL_NOT_FOUND` 意味着文档没读到，因此不能变成证据。"""
    messages = [
        tool_call("c1", "get_repository_content"),
        observation("c1", "get_repository_content", failure()),
        tool_call("c2", "get_repository_profile"),
        observation("c2", "get_repository_profile", failure("TOOL_ACCESS_DENIED")),
    ]

    evidence = _collect_evidence(messages)

    assert evidence.contents == {}
    assert evidence.profiles == {}
    assert evidence.repo_ids() == []


def test_a_successful_read_is_still_absorbed() -> None:
    """这是对照组：过滤失败不能连成功一起过滤掉。"""
    messages = [
        tool_call("c1", "get_repository_content"),
        observation("c1", "get_repository_content", content_payload()),
        tool_call("c2", "get_repository_profile"),
        observation("c2", "get_repository_profile", profile_payload()),
    ]

    evidence = _collect_evidence(messages)

    assert list(evidence.contents) == [REPO_ID]
    assert list(evidence.profiles) == [REPO_ID]


def test_a_failed_read_never_becomes_a_readme_citation() -> None:
    """这是用户可见的后果：不会去引用一份没人读过的文档。"""
    failed = [
        tool_call("c1", "get_repository_content"),
        observation("c1", "get_repository_content", failure()),
    ]
    succeeded = [
        tool_call("c1", "get_repository_content"),
        observation("c1", "get_repository_content", content_payload()),
    ]

    assert readme_source_refs(failed) == set()
    assert readme_source_refs(succeeded) == {f"repository_contents:{REPO_ID}:readme"}


def test_a_later_success_replaces_an_earlier_failure() -> None:
    """证明模型可以重试同一个仓库；只有成功的那次读取才算证据。"""
    messages = [
        tool_call("c1", "get_repository_content"),
        observation("c1", "get_repository_content", failure()),
        tool_call("c2", "get_repository_content"),
        observation("c2", "get_repository_content", content_payload()),
    ]

    evidence = _collect_evidence(messages)

    assert list(evidence.contents) == [REPO_ID]
    assert readme_source_refs(messages) == {f"repository_contents:{REPO_ID}:readme"}


def test_a_failed_metric_query_does_not_become_an_empty_series() -> None:
    """证明取列表的那几个分支不受影响，并且继续保持不受影响。"""
    messages = [
        tool_call("c1", "query_repo_metrics"),
        observation("c1", "query_repo_metrics", failure("TOOL_NOT_FOUND")),
        tool_call("c2", "query_repo_metrics"),
        observation(
            "c2",
            "query_repo_metrics",
            [
                {"snapshot_date": "2026-10-15", "stars": 10},
                {"snapshot_date": "2026-10-16", "stars": 12},
            ],
        ),
    ]

    evidence = _collect_evidence(messages)

    assert list(evidence.metrics) == [REPO_ID]
    assert len(evidence.metrics[REPO_ID]) == 2


def test_the_failure_predicate_tells_a_shell_from_a_payload() -> None:
    """这个谓词是各处共用的判断，所以单独钉住。"""
    assert is_tool_error_payload(tool_error_payload("TOOL_NOT_FOUND", "gone"))
    assert is_tool_error_payload({"error": {"code": "X"}})
    assert not is_tool_error_payload(content_payload())
    assert not is_tool_error_payload([{"error": {"code": "X"}}])
    assert not is_tool_error_payload({"error": "a string is not the shell"})
    assert not is_tool_error_payload("not an object")
    assert not is_tool_error_payload(None)


def series(*counts: tuple[int, int]) -> list[dict[str, Any]]:
    """用 `(日, stars)` 对构造一条指标序列。"""
    return [{"snapshot_date": f"2026-10-{day:02d}", "stars": stars} for day, stars in counts]


def evidence_messages(
    series_by_repo: dict[int, list[dict[str, Any]]],
    *,
    read: tuple[int, ...] = (),
) -> list[ChatMessage]:
    """为多个仓库的指标构造观测轮次，并附上需要读取的 README。"""
    messages: list[ChatMessage] = []
    call_index = 0
    for repo_id, points in series_by_repo.items():
        call_index += 1
        messages.append(tool_call(f"m{call_index}", "query_repo_metrics", repo_id))
        messages.append(observation(f"m{call_index}", "query_repo_metrics", points))
    for repo_id in read:
        call_index += 1
        messages.append(tool_call(f"r{call_index}", "get_repository_content", repo_id))
        messages.append(observation(f"r{call_index}", "get_repository_content", content_payload()))
    return messages


def sections_by_key(messages: list[ChatMessage]) -> dict[str, str]:
    """返回已组装草稿的 `section_key -> markdown` 映射。"""
    sections = compose_sections(_collect_evidence(messages))
    return {section["section_key"]: section["content_markdown"] for section in sections}


def test_no_section_repeats_another_line_for_line() -> None:
    """证明六个章节里曾有两节把同一句话说两遍。

    「本周摘要」点名了涨幅最大的几个，「重点项目」又用同样的标签、同样的增量复述一遍，
    于是一份六节的报告只承载了五节的信息量。每一节都得挣到自己的位置：摘要给结论，
    趋势节给方向，亮点节给各仓库在总变动中占的份额。
    """
    body = sections_by_key(
        evidence_messages(
            {
                1: series((13, 100), (14, 140), (15, 200), (16, 280)),
                2: series((13, 500), (14, 500), (15, 501), (16, 500)),
                3: series((13, 900), (14, 880), (15, 860), (16, 840)),
            }
        )
    )

    lines = {
        key: [line for line in text.splitlines() if line.strip()] for key, text in body.items()
    }
    summary, trend, highlights = lines["summary"], lines["trend"], lines["highlights"]

    assert not set(summary) & set(highlights)
    assert not set(trend) & set(highlights)
    # 亮点节仍然得对涨得最猛的那几个说点什么。
    assert len(highlights) == 1
    assert "占本期合计净增的" in highlights[0]


def test_highlights_rank_by_share_of_the_topic_movement() -> None:
    """证明亮点行带着趋势行没有的信息：份额。"""
    body = sections_by_key(
        evidence_messages(
            {
                1: series((13, 100), (14, 200), (15, 300), (16, 400)),
                2: series((13, 100), (14, 100), (15, 101), (16, 102)),
            }
        )
    )

    highlights = body["highlights"]
    # 仓库 1 涨了 300，仓库 2 涨了 2，合计 302，仓库 1 占 99%。
    assert "净增 300 stars" in highlights
    assert "占本期合计净增的 99%" in highlights


def test_an_unread_readme_is_not_reported_as_missing() -> None:
    """证明风险行必须只陈述这份代码知道的事：README 没被读过。

    它原本写「没有缓存到 README」，那是关于 `repository_contents` 的断言，而组装器
    从不检查那张表。离线规划器在预算用尽后就停止读正文，于是一份已经缓存好的 README
    会被报成不存在 —— 一个出现在已发布报告里的事实错误。
    """
    body = sections_by_key(
        evidence_messages(
            {
                1: series((13, 100), (14, 200), (15, 300), (16, 400)),
                2: series((13, 500), (14, 500), (15, 500), (16, 500)),
            },
            read=(1,),
        )
    )

    risks = body["risks"]
    assert "未读取 README" in risks
    assert "没有缓存到" not in risks
