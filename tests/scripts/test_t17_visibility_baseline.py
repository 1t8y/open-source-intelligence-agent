"""T17 的授权预期必须量出来，不能想当然。

T17 演练灌入四份报告，然后断言每个账号能触达其中哪几份。
最直观的写法 —— 拿 API 的答案跟演练刚创建的 id 比，并把 id 写成字面量
（`'1'`、`'1,2,3'`、`'2,4'`）—— 暗含一个前提：库里报告 id 从 1 开始。
这只有在从没人写过数据的卷上才成立。

`scripts/verify_t17.ps1` 从不删卷，而 MySQL 在删行时不会重置
`AUTO_INCREMENT`，所以同一个卷上*第二次*运行演练时，
报告的编号从上一轮停下的地方接着走 —— 演练的清理删的是自己的行，不是自己的 id。
于是这套字面量在全新的卷上通过、在用过一段时间的卷上失败，
两边代码完全一样，而失败看起来像访问控制缺陷，实际上是个算术问题。

`measure_visibility_baseline` 量的是演练写入之前各账号本就能读到什么，
`visibility_expectations` 再把它与演练创建的 id 合并起来。这些用例把两者都钉住，
第二个用例还把旧的字面量原样写出来，让这个缺陷无法再悄悄回来。
"""

from __future__ import annotations

import pytest
import t17_security_drill
from sqlalchemy.orm import Session, sessionmaker
from support import NOW, Workspace

from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.reporting import Report
from open_source_intelligence.security.authorization import (
    VISIBILITY_INTERNAL,
    VISIBILITY_PRIVATE,
    VISIBILITY_TEAM,
)

#: 演练里编辑者所代理的账号：它拥有其中一份私有报告。
#: id 取大一些，好让这些行与 `Workspace` 灌主题时为自己创建的那些行错开。
OWNER_ID = 100
#: 别人的 id，用于那分编辑者*绝不能*触达的私有报告。
STRANGER_ID = 200


def _add_user(session: Session, *, user_id: int, username: str) -> None:
    session.add(User(id=user_id, username=username, password_hash="hash", display_name=username))
    session.flush()


def _add_report(
    session: Session,
    *,
    topic_id: int,
    agent_run_id: int,
    title: str,
    visibility: str,
    owner_id: int | None = None,
) -> int:
    """插入一份报告，显式指定可见性与归属，并返回它的 id。"""
    report = Report(
        agent_run_id=agent_run_id,
        topic_id=topic_id,
        owner_id=owner_id,
        title=title,
        current_version=1,
        content_markdown=f"# {title}\n",
        status="published",
        visibility=visibility,
        sensitivity="INTERNAL",
        publish_mode="AUTO_WITH_DELAY",
        generated_at=NOW,
        published_at=NOW,
    )
    session.add(report)
    session.flush()
    return int(report.id)


def _seed_world(workspace: Workspace) -> dict[str, int]:
    """灌入访问规则所区分的四种既有情形，并返回它们的 id。"""
    session = workspace.session
    _add_user(session, user_id=OWNER_ID, username="existing-owner")
    _add_user(session, user_id=STRANGER_ID, username="existing-stranger")
    topic = workspace.add_topic(name="existing-topic")
    run = workspace.add_agent_run(topic.id)
    return {
        "internal": _add_report(
            session,
            topic_id=topic.id,
            agent_run_id=run.id,
            title="existing internal",
            visibility=VISIBILITY_INTERNAL,
        ),
        "team": _add_report(
            session,
            topic_id=topic.id,
            agent_run_id=run.id,
            title="existing team",
            visibility=VISIBILITY_TEAM,
        ),
        "owned": _add_report(
            session,
            topic_id=topic.id,
            agent_run_id=run.id,
            title="existing owned private",
            visibility=VISIBILITY_PRIVATE,
            owner_id=OWNER_ID,
        ),
        "stranger": _add_report(
            session,
            topic_id=topic.id,
            agent_run_id=run.id,
            title="existing stranger private",
            visibility=VISIBILITY_PRIVATE,
            owner_id=STRANGER_ID,
        ),
    }


def _seed_drill_reports(workspace: Workspace) -> dict[str, int]:
    """经由演练自己的插入函数，灌入演练自己的那四份报告。

    `seed_reports` 在写报告之前要先建主题、Agent 运行与 Agent 配置；
    预期是从报告本身推出来的，所以这里只灌报告，并对每一份调用
    `t17_security_drill._seed_report`，而不是在这儿把四行复述一遍。
    演练若改了哪份是 `INTERNAL`、哪份私有归谁，这里跟着走。
    """
    session = workspace.session
    topic = workspace.add_topic(name="drill-topic")
    run = workspace.add_agent_run(topic.id)
    return {
        "internal": t17_security_drill._seed_report(
            session,
            topic_id=topic.id,
            agent_run_id=run.id,
            title=t17_security_drill.REPORT_TITLES["internal"],
            visibility=VISIBILITY_INTERNAL,
            owner_id=None,
        ),
        "team": t17_security_drill._seed_report(
            session,
            topic_id=topic.id,
            agent_run_id=run.id,
            title=t17_security_drill.REPORT_TITLES["team"],
            visibility=VISIBILITY_TEAM,
            owner_id=None,
        ),
        "private_editor": t17_security_drill._seed_report(
            session,
            topic_id=topic.id,
            agent_run_id=run.id,
            title=t17_security_drill.REPORT_TITLES["private_editor"],
            visibility=VISIBILITY_PRIVATE,
            owner_id=OWNER_ID,
        ),
        "private_orphan": t17_security_drill._seed_report(
            session,
            topic_id=topic.id,
            agent_run_id=run.id,
            title=t17_security_drill.REPORT_TITLES["private_orphan"],
            visibility=VISIBILITY_PRIVATE,
            owner_id=None,
        ),
    }


def test_baseline_classifies_pre_existing_reports_by_the_documented_rule(
    session_factory: sessionmaker[Session],
) -> None:
    """证明基线按落库的那些列，重述了 `docs/security-and-privacy.md` §3.3。

    某个角色的基线必须是它在演练写入*之前*就能读到的行：
    管理员全拿，编辑者在共有的 `INTERNAL` 之外再加 `TEAM` 与自己的 `PRIVATE`，
    只读者只有 `INTERNAL`。别人的私有报告，正是用来区分
    「比对归属」的谓词与「只检查归属非空」的谓词的那一行。
    """
    with session_factory() as session:
        workspace = Workspace(session)
        existing = _seed_world(workspace)
        session.commit()

        baseline = t17_security_drill.measure_visibility_baseline(session, editor_id=OWNER_ID)

    assert baseline.admin.ids == tuple(sorted(existing.values())), "every report"
    assert baseline.admin.total == 4
    assert baseline.editor.ids == tuple(
        sorted((existing["internal"], existing["team"], existing["owned"]))
    ), "the internal, team, and owned private reports"
    assert existing["stranger"] not in baseline.editor.ids, "a private report it does not own"
    assert baseline.editor.total == 3
    assert baseline.viewer.ids == (existing["internal"],), "the internal report alone"
    assert baseline.viewer.total == 1


def test_expectations_are_built_from_the_ids_and_not_from_literals(
    session_factory: sessionmaker[Session],
) -> None:
    """证明预期是跟着行走的，因此带历史的库也打不垮它。

    既有报告把演练自己的 id 顶到 `1` 之后，正是 `verify_t17.ps1`
    跑第二遍时留下的状态。最后一块把旧字面量直接写出来：
    在这个库上它们每一条都是假的，照它写的检查会报出一个根本没发生过的授权失败。
    """
    with session_factory() as session:
        workspace = Workspace(session)
        existing = _seed_world(workspace)
        session.commit()

        baseline = t17_security_drill.measure_visibility_baseline(session, editor_id=OWNER_ID)
        reports = _seed_drill_reports(workspace)
        session.commit()

        visible, totals = t17_security_drill.visibility_expectations(reports, baseline)

    assert reports["internal"] > max(existing.values()), "the drill's ids continue the table's ids"
    assert visible["admin"] == sorted((*existing.values(), *reports.values())), "all eight reports"
    assert totals["admin"] == 8
    assert visible["editor"] == sorted(
        (
            existing["internal"],
            existing["team"],
            existing["owned"],
            reports["internal"],
            reports["team"],
            reports["private_editor"],
        )
    )
    assert totals["editor"] == 6
    assert visible["viewer"] == sorted((existing["internal"], reports["internal"]))
    assert totals["viewer"] == 2

    assert ",".join(str(value) for value in visible["viewer"]) != "1"
    assert ",".join(str(value) for value in visible["editor"]) != "1,2,3"


def test_baseline_refuses_a_page_that_cannot_hold_the_answer(
    session_factory: sessionmaker[Session],
) -> None:
    """证明某角色能读的报告已超过一页时会被直接拒绝，而不是悄悄截断。

    演练一页一页地列报告，而 `GET /api/v1/reports` 把 `page_size` 卡在
    `MAX_LIST_PAGE`，一旦越过这个上限，它用来比对的 id
    就不再是该角色可读的全部。
    拿被截断的一页去比对完整的预期，失败原因会跟访问规则毫无关系，
    所以演练停在这里 —— 并说明是哪个账号量不了、表有多满。
    """
    with session_factory() as session:
        workspace = Workspace(session)
        topic = workspace.add_topic()
        run = workspace.add_agent_run(topic.id)
        overflow = t17_security_drill.MAX_LIST_PAGE + 1
        # 每一行都是 `INTERNAL`，所以每个角色都超了上限，
        # 先被量到的那个就是被点名的那个。
        # 具体是哪一个并不重要，重要的是它会拒绝。
        session.add_all(
            [
                Report(
                    agent_run_id=run.id,
                    topic_id=topic.id,
                    title=f"bulk internal {index}",
                    current_version=1,
                    content_markdown="body",
                    status="published",
                    visibility=VISIBILITY_INTERNAL,
                    sensitivity="INTERNAL",
                    publish_mode="AUTO_WITH_DELAY",
                    generated_at=NOW,
                    published_at=NOW,
                )
                for index in range(overflow)
            ]
        )
        session.commit()

        with pytest.raises(RuntimeError) as raised:
            t17_security_drill.measure_visibility_baseline(session, editor_id=OWNER_ID)

    message = str(raised.value)
    assert "admin" in message, "the refusal names the account it could not measure"
    assert str(overflow) in message, "the refusal says how full the table is"


class _StubResponse:
    """`_api_readable_ids` 从列表响应上读取的那两个属性。"""

    status_code = 200
    text = ""

    @staticmethod
    def json() -> dict[str, object]:
        return {"items": [{"id": 7}], "total": 1}


class _RecordingClient:
    """`TestClient` 的替身，记录它被请求的 URL。"""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str, headers: dict[str, str] | None = None) -> _StubResponse:
        self.urls.append(url)
        return _StubResponse()


def test_the_drill_lists_a_full_page() -> None:
    """证明基线所依据的上限，就是演练真正请求的那一页。

    一旦某角色可读的行数超过列表响应能装下的数量，
    `measure_visibility_baseline` 就会拒绝。只有演练请求的是整页时，
    这个上限才是对的：请求更少，则要等到答案早已装不进一条响应才触发拒绝；
    请求更多，则是被端点自身的上限打成 `422`。
    """
    client = _RecordingClient()

    ids, total = t17_security_drill._api_readable_ids(client, {})  # type: ignore[arg-type]

    assert ids == [7]
    assert total == 1
    assert f"page_size={t17_security_drill.MAX_LIST_PAGE}" in client.urls[0]
