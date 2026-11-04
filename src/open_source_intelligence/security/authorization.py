"""报告授权：唯一决定谁能读、改、审、发布的地方。

`docs/security-and-privacy.md` §3.3 要求把权限判定集中起来，而不是散落在各个路由里，
因此请求时的守卫与列表查询的谓词都出自这里。

初始模型刻意做得很小：

* `ADMIN` 可读所有报告。
* `INTERNAL` 可见性对任何已认证用户可读。
* `TEAM` 可见性还要求 `data_scope = team`。
* `PRIVATE` 可见性要求是记录在案的属主。没有属主的报告仅对 `ADMIN` 开放，对于一行从未
  设置过属主的记录，这才是对「私密」的安全解读。

`readable_reports_filter` 把同一条规则表达成 SQL 谓词，让列表查询把判定下推到数据库，而
不是先取出行再在事后过滤。
"""

from __future__ import annotations

from typing import Final

from fastapi import HTTPException, Request, status
from sqlalchemy import ColumnElement, and_, or_, true
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import Topic
from open_source_intelligence.persistence.models.reporting import Report
from open_source_intelligence.security.dependencies import deny_request
from open_source_intelligence.security.service import get_role_codes

ROLE_ADMIN: Final[str] = "ADMIN"
ROLE_EDITOR: Final[str] = "EDITOR"
ROLE_VIEWER: Final[str] = "VIEWER"

#: 允许写入的角色：校验、编辑、审核、发布与回滚。
REPORT_WRITE_ROLES: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_EDITOR)
#: 有资格读取任何报告的角色。
REPORT_READ_ROLES: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER)

#: 允许读取运维总览与 Agent 运行流水的角色。这些界面会暴露跨主题的成本、失败原因与评测
#: 结论，因此它们归运维角色而不是归报告读者 —— viewer 看的是报告，不是产出报告的那本流水。
METRICS_ROLES: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_EDITOR)

#: 允许读取仓库目录的角色。目录里是公开的仓库元数据（`docs/security-and-privacy.md` §2
#: 将其归为公开），因此每个已认证角色都能读；需要裁定的不是目录本身，而是它可以按谁的
#: 主题来切分，那由 `can_read_topic` 决定。
REPOSITORY_READ_ROLES: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER)

#: 允许读取审计日志的角色。读它本身就是一种特权：每一条记录都写明了平台上每一次被拒绝的
#: 请求、每一次配置变更和每一次发布。
AUDIT_READ_ROLES: Final[tuple[str, ...]] = (ROLE_ADMIN,)

#: 允许看到主题列表的角色。这道门只负责把匿名调用方挡在外面；调用方*能看哪些*主题由
#: `can_read_topic` 决定，因为被禁用的主题属于未发布的配置，而不是别人的私密数据。这个集合
#: 与报告、目录的读集合一致：读主题并不比读报告更有特权。
TOPIC_READ_ROLES: Final[tuple[str, ...]] = (ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER)

#: 允许修改主题的角色。`EDITOR` 的写权限只覆盖报告，而主题不是报告：它决定采集哪些仓库，
#: 也就决定了下一次每日运行要花掉多大的模型预算、消耗多少公共 API 配额。`docs/api-contract.md`
#: §3.1 拒绝提供 `POST /sources` 也是同一个理由 —— 一个能挪动限流预算的端点，等于把用量控制权
#: 交给任何已认证的客户端。
TOPIC_WRITE_ROLES: Final[tuple[str, ...]] = (ROLE_ADMIN,)

TEAM_DATA_SCOPE: Final[str] = "team"

VISIBILITY_PRIVATE: Final[str] = "PRIVATE"
VISIBILITY_TEAM: Final[str] = "TEAM"
VISIBILITY_INTERNAL: Final[str] = "INTERNAL"


def can_read_report(user: User, report: Report, roles: set[str]) -> bool:
    """判断 `user` 是否可以读 `report`。

    这是读取规则作用在单条记录上的入口；`readable_reports_filter` 是同一条规则作用在 SQL
    上的入口。两处入口，一条规则，必须始终一致。
    """
    if ROLE_ADMIN in roles:
        return True
    if report.visibility == VISIBILITY_INTERNAL:
        return True
    if report.visibility == VISIBILITY_TEAM:
        return user.data_scope == TEAM_DATA_SCOPE
    return report.owner_id is not None and report.owner_id == user.id


def readable_reports_filter(user: User, roles: set[str]) -> ColumnElement[bool]:
    """把访问规则表达成报告列表查询用的 SQL 谓词。

    这是与 `can_read_report` 同一条规则的第二个入口：一处判定单条记录，一处交给数据库筛
    选整个列表。两处入口，一条规则 —— 两边一旦分头实现，列表就会开始显示调用方其实读不
    到的行。
    """
    if ROLE_ADMIN in roles:
        return true()
    conditions: list[ColumnElement[bool]] = [Report.visibility == VISIBILITY_INTERNAL]
    if user.data_scope == TEAM_DATA_SCOPE:
        conditions.append(Report.visibility == VISIBILITY_TEAM)
    conditions.append(and_(Report.visibility == VISIBILITY_PRIVATE, Report.owner_id == user.id))
    return or_(*conditions)


def require_report_read(
    session: Session,
    request: Request,
    user: User,
    report: Report,
) -> None:
    """当前用户无权读取报告时，拒绝请求并写入审计。

    403 响应体不携带任何报告元数据，因此被拒绝的请求对一份自己看不到的报告一无所知。审计
    行则正好相反：它*确实*写明是哪份报告，因为一条说不出被拒资源是什么的内部流水，无法回
    答「谁试图读什么」—— 而这正是拒绝记录唯一存在的意义。
    """
    if can_read_report(user, report, get_role_codes(user)):
        return
    deny_request(
        session,
        request,
        action="report.read",
        resource_type="report",
        resource_id=str(report.id),
        status_code=status.HTTP_403_FORBIDDEN,
        detail="insufficient permissions for this report",
        actor_id=user.id,
    )


def authorize_report_read(
    session: Session,
    request: Request,
    user: User,
    *,
    report_id: int,
) -> Report:
    """加载一份报告供读取，同时执行 ACL 并把失败映射为 HTTP 状态码。"""
    report = session.get(Report, report_id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"report {report_id} does not exist",
        )
    require_report_read(session, request, user, report)
    return report


def can_read_topic(user: User, topic: Topic, roles: set[str]) -> bool:
    """判断 `user` 是否可以按 `topic` 切分仓库目录。

    仓库目录本身是公开的，所以这不是容器级 ACL。它守的是成员关系问题：某个主题跟踪哪些
    仓库属于内部策略，而 `topic_repositories` 正是这件事变得可观测的地方。

    `ADMIN` 可读所有主题。其他人可读 `enabled` 的主题 —— 规则就是这一条，刻意如此。
    `topics` 没有 ACL 列，凭空造一个按主题的授权会是新功能（`REQUIREMENTS.md` 已把
    完整的 ABAC 划出范围），而不是文档同步。被禁用的主题被当作未发布的配置，而不是
    别人的私密数据，因此这道门是角色检查，而不是属主检查。
    """
    if ROLE_ADMIN in roles:
        return True
    return topic.enabled


def readable_topics_filter(user: User, roles: set[str]) -> ColumnElement[bool]:
    """把主题可见性规则表达成主题列表查询用的 SQL 谓词。

    这是 `can_read_topic` 的谓词形式，而且必须一直是它的谓词形式。契约里两条看似不同
    的要求其实是同一条：规则不得按界面各实现一遍，列表的 `total` 也必须在同一条规则
    下计算 —— 先取回所有主题、再在事后丢掉看不见的那些，统计出来的仍会是调用方无权
    看到的主题数量。`tests/security/test_authorization.py` 会逐行把这个谓词与
    `can_read_topic` 比对，而不是指望两者靠「一起改」来保持一致。
    """
    if ROLE_ADMIN in roles:
        return true()
    return Topic.enabled.is_(True)


def authorize_topic_read(
    session: Session,
    request: Request,
    user: User,
    *,
    topic_id: int,
    action: str = "repository.read",
) -> Topic:
    """加载一个主题用于目录筛选，否则拒绝请求。

    失败的分工与报告资源一致：主题不存在是 `404`，主题存在但不可见是 `403`。
    `403` 响应体不携带任何主题元数据，而审计行会写明是哪个主题 —— 与报告 ACL 相同的
    镜像关系，理由也相同。

    `action` 之所以是参数，是因为同一条规则有两个界面：仓库目录按主题筛选并记录
    `repository.read`，而主题端点自己记录 `topic.read`。默认值保持了较早调用方的行为，
    让第二个界面用自己的动作名，而不是继承一个描述着另一种请求的名字。
    """
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"topic {topic_id} does not exist",
        )
    if can_read_topic(user, topic, get_role_codes(user)):
        return topic
    deny_request(
        session,
        request,
        action=action,
        resource_type="topic",
        resource_id=str(topic.id),
        status_code=status.HTTP_403_FORBIDDEN,
        detail="insufficient permissions for this topic",
        actor_id=user.id,
    )
