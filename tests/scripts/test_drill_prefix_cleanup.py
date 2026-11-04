"""演练脚本的清理只能删掉它自己确证属于自己的那些行。

每套冒烟脚本都给自己创建的行打上名字前缀（`__t7_smoke_`、
`__T15 report__`、`__t17_drill_` ……），随后按该前缀删掉「自己的」行。
最直观的写法 `Column.like(f"{prefix}%")` 并不等于「以这个前缀开头」：
`_` 在 `LIKE` 里是单字符通配符，而这些前缀里每一个都含 `_`，
于是这个模式连演练从未写过的值也能匹配上。
一个会删掉别人数据的清理，比留下自己的残留更糟，
所以现在每个演练都按**字面**前缀匹配（`startswith(..., autoescape=True)`）。

这些用例驱动的是演练自己的清理函数 —— 不是它们谓词的副本 ——
对着一个只放两行的数据库跑：一行按演练标记自己数据的方式命名，
另一行只有 `LIKE` 模式才会命中。前者必须被删掉，后者必须活下来。
换回旧的 `like(f"{prefix}%")` 写法，下面每个用例都会失败，这正是重点：
留下自己的行只是脏，误删别人的行是缺陷。

用例覆盖演练里十三处*按前缀删除*的位置。其余按前缀匹配的地方都是计数
（`t16._repo_count`、`t17` 里的注入计数与重复计数），刻意没有纳入：
计数过宽不会删掉任何东西，只会让演练自己的断言不一致并大声失败；
而删除过宽是无声的，会毁掉一条属于别人的行。
两者用的是同一种写法，所以这些用例钉住的转义就是它们用的转义。

这些脚本能被导入，是因为 `pyproject.toml` 把 `scripts/` 放进了 pytest 的
`pythonpath`；其中好几个本来就是用裸模块名互相导入的。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import pytest
import t7_mysql_smoke
import t8_mysql_smoke
import t11_mysql_smoke
import t13_mysql_smoke
import t14_mysql_smoke
import t15_mysql_smoke
import t16_mysql_smoke
import t17_security_drill
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace

from open_source_intelligence.persistence.models.agent import AgentConfig
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import Repository
from open_source_intelligence.persistence.models.notifications import PushTarget
from open_source_intelligence.persistence.models.reporting import Report

# --------------------------------------------------------------------------------------
# 演练按哪个标签列清理，就给它配一套灌数据函数与读取函数
# --------------------------------------------------------------------------------------


def _seed_repository(workspace: Workspace, labels: Sequence[str]) -> None:
    source = workspace.add_source()
    for label in labels:
        workspace.add_repository(
            source_id=source.id, provider_repo_id=label, full_name=f"acme/{label}"
        )


def _repositories(session: Session) -> set[str]:
    return set(session.scalars(select(Repository.provider_repo_id)))


def _seed_report(workspace: Workspace, labels: Sequence[str]) -> None:
    topic = workspace.add_topic()
    run = workspace.add_agent_run(topic.id)
    for label in labels:
        workspace.add_report(topic_id=topic.id, agent_run_id=run.id, title=label)


def _reports(session: Session) -> set[str]:
    return set(session.scalars(select(Report.title)))


def _seed_user(workspace: Workspace, labels: Sequence[str]) -> None:
    for label in labels:
        workspace.session.add(User(username=label, password_hash="hash", display_name=label))
    workspace.session.flush()


def _users(session: Session) -> set[str]:
    return set(session.scalars(select(User.username)))


def _seed_agent_config(workspace: Workspace, labels: Sequence[str]) -> None:
    for label in labels:
        workspace.session.add(
            AgentConfig(
                name=label,
                version="1",
                provider="offline",
                model="offline",
                prompt_version="v1",
                tool_policy_json={},
            )
        )
    workspace.session.flush()


def _agent_configs(session: Session) -> set[str]:
    return set(session.scalars(select(AgentConfig.name)))


def _seed_push_target(workspace: Workspace, labels: Sequence[str]) -> None:
    for label in labels:
        workspace.add_push_target(name=label)


def _push_targets(session: Session) -> set[str]:
    return set(session.scalars(select(PushTarget.name)))


def _seed_t15_reports(workspace: Workspace, labels: Sequence[str]) -> None:
    """用唯一能被 T15 清理触达的方式灌报告：靠它的标题谓词。

    T15 按标题删除的语句嵌在 `if topic_ids:` 里，而那些 id 来自它自己的主题名，
    所以那个主题必须存在，否则谓词根本不会执行。但这些报告仍要挂到另一个主题下：
    挂到演练自己的主题下，`topic_id IN (...)` 那条删除一样会把它们删掉，
    这个用例就会在标题谓词完全不正确的情况下通过。
    """
    workspace.add_topic(name=t15_mysql_smoke.TOPIC_NAME)
    carrying = workspace.add_topic(name="t15-unrelated-topic")
    run = workspace.add_agent_run(carrying.id)
    for label in labels:
        workspace.add_report(topic_id=carrying.id, agent_run_id=run.id, title=label)


# --------------------------------------------------------------------------------------
# 需要操作者映射的清理；传空映射走的是「没有操作者」那条路径
# --------------------------------------------------------------------------------------


def _t13_cleanup(session: Session) -> None:
    t13_mysql_smoke._cleanup(session, {})


def _t14_cleanup(session: Session) -> None:
    t14_mysql_smoke._cleanup(session, {})


def _t15_cleanup(session: Session) -> None:
    t15_mysql_smoke._cleanup(session, {})


# --------------------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Drill:
    """一轮清理、它自己的前缀，以及一个只有 `LIKE` 才会命中的值。"""

    name: str
    prefix: str
    adversary: str
    cleanup: Callable[[Session], None]
    seed: Callable[[Workspace, Sequence[str]], None]
    labels: Callable[[Session], set[str]]


#: 下面每个 `adversary` 都拿 `SELECT ? LIKE ?` 对着演练的旧模式核过；
#: 每一个都满足 `LIKE f"{prefix}%"`，却并不以 `prefix` 开头。
CASES = (
    Drill(
        name="t7-repositories",
        prefix="__t7_smoke_",
        adversary="aat7xsmokey1",
        cleanup=t7_mysql_smoke.cleanup_verification_records,
        seed=_seed_repository,
        labels=_repositories,
    ),
    Drill(
        name="t7-agent-configs",
        prefix="__t7_smoke_",
        adversary="aat7xsmokey1",
        cleanup=t7_mysql_smoke.cleanup_verification_records,
        seed=_seed_agent_config,
        labels=_agent_configs,
    ),
    Drill(
        name="t8-repositories",
        prefix="__t8_smoke_",
        adversary="aat8xsmokey1",
        cleanup=t8_mysql_smoke.cleanup_verification_records,
        seed=_seed_repository,
        labels=_repositories,
    ),
    Drill(
        name="t11-reports",
        prefix="__T11",
        adversary="abT11foo",
        cleanup=t11_mysql_smoke._cleanup,
        seed=_seed_report,
        labels=_reports,
    ),
    Drill(
        name="t13-push-targets",
        prefix="__T13",
        adversary="abT13foo",
        cleanup=_t13_cleanup,
        seed=_seed_push_target,
        labels=_push_targets,
    ),
    Drill(
        name="t14-repositories",
        prefix="__t14_smoke_",
        adversary="aat14xsmokey1",
        cleanup=_t14_cleanup,
        seed=_seed_repository,
        labels=_repositories,
    ),
    Drill(
        name="t15-reports",
        prefix="__T15 report__",
        adversary="abT15 reportzz",
        cleanup=_t15_cleanup,
        seed=_seed_t15_reports,
        labels=_reports,
    ),
    Drill(
        name="t15-repositories",
        prefix="__t15_smoke_",
        adversary="aat15xsmokey1",
        cleanup=_t15_cleanup,
        seed=_seed_repository,
        labels=_repositories,
    ),
    Drill(
        name="t16-repositories",
        prefix="__t16_smoke_",
        adversary="aat16xsmokey1",
        cleanup=t16_mysql_smoke.cleanup_verification_records,
        seed=_seed_repository,
        labels=_repositories,
    ),
    Drill(
        name="t17-reports",
        prefix="__T17",
        adversary="abT17foo",
        cleanup=t17_security_drill.purge_drill_rows,
        seed=_seed_report,
        labels=_reports,
    ),
    Drill(
        name="t17-users",
        prefix="__t17_",
        adversary="abt17c_smith",
        cleanup=t17_security_drill.purge_drill_rows,
        seed=_seed_user,
        labels=_users,
    ),
    Drill(
        name="t17-push-targets",
        prefix="__t17_",
        adversary="abt17xtarget",
        cleanup=t17_security_drill.purge_drill_rows,
        seed=_seed_push_target,
        labels=_push_targets,
    ),
    Drill(
        name="t17-repositories",
        prefix="__t17_drill_",
        adversary="aat17xdrilly1",
        cleanup=t17_security_drill.purge_drill_rows,
        seed=_seed_repository,
        labels=_repositories,
    ),
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_a_cleanup_removes_its_own_rows_and_leaves_the_rest(
    case: Drill, session_factory: sessionmaker[Session]
) -> None:
    """证明演练删掉的是自己那行，而只有 `LIKE` 才命中的值留了下来。

    两行都带着看起来说得通的标签。第一行是 `{prefix}genuine`，
    每套演练都用这种方式命名自己创建的数据，任何前缀匹配都能找到它。
    第二行满足旧的 `LIKE` 模式却不以该前缀开头 ——
    那是演练根本没有主张权利的一行。
    """
    own = f"{case.prefix}genuine"
    with session_factory() as session:
        workspace = Workspace(session)
        case.seed(workspace, (own, case.adversary))
        session.commit()

        case.cleanup(session)

        remaining = case.labels(session)
        assert own not in remaining, "the cleanup left a row of its own behind"
        assert case.adversary in remaining, "the cleanup deleted a row the drill never wrote"
