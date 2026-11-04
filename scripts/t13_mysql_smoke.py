"""针对 MySQL 跑 T13 的邮件 / Webhook 推送冒烟。

脚本复用 T8 的种子辅助函数与 T11 的 CLI 运行器，然后把*真正的*报告 CLI、
*真正的* FastAPI 应用与*真正的* Jinja2/HTMX 管理界面跑在 MySQL 上，而不是内存
SQLite 里。它端到端覆盖 T13 的验收标准：

1. **只有 `approved` 或 `published` 的报告可以推送** —— `draft` 与
   `pending_review` 都会被拒，且各自返回同一个稳定的错误码；
2. **只有白名单里的目的地能收到东西** —— 未知目标、已停用的目标，以及渠道与
   目标行不符的请求，一律拒绝；
3. **敏感材料需要审批** —— `PRIVATE`/`CONFIDENTIAL` 报告在人工批准*且*目标被
   显式放行这两项都满足之前都会被拒；一份 `CONFIDENTIAL` 报告即使目标已放行、
   但没有审批，仍然会被拒；
4. **投递幂等且有记录** —— 同一个（报告、版本、目标）三元组只投递一次，重复
   请求走重放，并留下轨迹；
5. **失败的投递可以重试** —— API 清扫与 CLI 清扫都会在原行上重新投递失败行、
   推进它的尝试次数、跳过不可用的行且不中断整批，并且不去碰已经耗尽预算的行；
6. **放行会自动扇出** —— 发布无需人工介入就推送到每个启用目标，报告最后仍然
   处于已发布状态。

脚本全程使用默认的 `push_delivery="offline"` 传输。这是刻意的：它是部署默认值，
而且不依赖可达的 SMTP 或 HTTP 设施，好让载荷保持确定性。`live` 传输改在它的
配置边界上覆盖 —— 未配置的 live 渠道必须在写入投递行*之前*就被拒，因为配置错误
的部署属于请求错误，而不是可重试的传输失败。

脚本断言的每一次数据库读取都经过一个*全新的*会话，所以断言通过不可能是靠写行
那个会话的身份映射解释掉的。
"""

import argparse
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import t8_mysql_smoke as t8
import t11_mysql_smoke as t11
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from open_source_intelligence.agent.runtime import run_research_agent
from open_source_intelligence.api.app import create_app
from open_source_intelligence.core.config import Settings, get_settings
from open_source_intelligence.notifications.channels import build_channel
from open_source_intelligence.notifications.errors import PushTargetNotAllowed
from open_source_intelligence.notifications.service import push_report
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.identity import User, UserRole
from open_source_intelligence.persistence.models.intelligence import PipelineRun
from open_source_intelligence.persistence.models.notifications import PushTarget, ReportPush
from open_source_intelligence.persistence.models.reporting import (
    Report,
    ReportReview,
    ReportSource,
    ReportVerification,
    ReportVersion,
)
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.security.passwords import hash_password
from open_source_intelligence.security.service import create_user
from open_source_intelligence.web.security import CSRF_COOKIE, SESSION_COOKIE

API = "/api/v1/reports"
PUSH_API = "/api/v1/push"
UI = "/ui/reports"

T13_NOW = datetime(2099, 9, 5, 12, 0, tzinfo=UTC)
SMOKE_PASSWORD = "t13-smoke-password-not-for-production"
ACTOR_PREFIX = "__t13_"
TARGET_PREFIX = "__T13"

PRIVATE_TITLE = "__T13 private confidential report__"
PUBLISHED_TITLE = "__T13 published report__"
CONFIDENTIAL_TITLE = "__T13 confidential without approval__"

#: `(suffix, channel, destination, enabled, allow_private, allow_confidential)`。
#: 除那个显式放行的目标外，所有目标的两个 `allow_*` 开关都是关的 —— 这正是「已放行
#: 目标」与「未放行目标」两条断言能成立的原因。
TARGET_SPECS = (
    ("email", "email", "t13-ops@example.com", True, False, False),
    ("webhook", "webhook", "https://hooks.example.com/t13", True, False, False),
    ("secondary", "email", "t13-secondary@example.com", True, False, False),
    ("disabled", "email", "t13-disabled@example.com", False, False, False),
    ("cleared", "email", "t13-cleared@example.com", True, True, True),
)

ROLES = {"admin": "ADMIN", "editor": "EDITOR", "viewer": "VIEWER"}
SCOPES = {"admin": "personal", "editor": "team", "viewer": "personal"}


def _target_name(suffix: str) -> str:
    """返回白名单里某一项的冒烟目标名。"""
    return f"{TARGET_PREFIX} {suffix}__"


def _username(suffix: str) -> str:
    """返回某个角色的冒烟用户名。"""
    return f"{ACTOR_PREFIX}{suffix}__"


def _refresh(session: Session) -> None:
    """CLI 在另一个进程里写入之后，丢弃本地缓存的行。

    只调 `rollback()` 不够：已经加载过的实例仍攥着旧的属性值，后续查询可能返回一行，
    而它的 ORM 状态早已和 SQL 谓词对不上。生产环境每个任务用一个短生命周期会话，
    不会暴露在这个问题下。
    """
    session.rollback()
    session.expire_all()


# --------------------------------------------------------------------------------------
# 播种与清理
# --------------------------------------------------------------------------------------


def _t13_report_ids(session: Session, actor_ids: dict[str, int]) -> list[int]:
    """返回上一轮或本轮 T13 运行名下的报告 id。"""
    ids = list(actor_ids.values())
    if not ids:
        return []
    return [
        int(value) for value in session.scalars(select(Report.id).where(Report.owner_id.in_(ids)))
    ]


def _cleanup(session: Session, actor_ids: dict[str, int]) -> None:
    """删掉一轮 T13 冒烟创建的全部行，再删它们底下的 T8 记录。

    顺序有讲究：`report_pushes.target_id` 是 `ON DELETE RESTRICT`，所以投递行必须
    先于它们指向的白名单条目被删掉。
    """
    report_ids = _t13_report_ids(session, actor_ids)
    if report_ids:
        session.execute(delete(ReportPush).where(ReportPush.report_id.in_(report_ids)))
        session.execute(delete(ReportReview).where(ReportReview.report_id.in_(report_ids)))
        session.execute(
            delete(ReportVerification).where(ReportVerification.report_id.in_(report_ids))
        )
        session.execute(delete(ReportSource).where(ReportSource.report_id.in_(report_ids)))
        session.execute(delete(ReportVersion).where(ReportVersion.report_id.in_(report_ids)))
        session.execute(
            delete(AuditLog).where(
                AuditLog.resource_type == "report",
                AuditLog.resource_id.in_([str(value) for value in report_ids]),
            )
        )
        session.execute(delete(Report).where(Report.id.in_(report_ids)))
    session.execute(
        delete(PushTarget).where(PushTarget.name.startswith(TARGET_PREFIX, autoescape=True))
    )
    ids = list(actor_ids.values())
    if ids:
        session.execute(delete(UserRole).where(UserRole.user_id.in_(ids)))
        session.execute(delete(User).where(User.id.in_(ids)))
    session.commit()
    t8.cleanup_verification_records(session)


def _actor_ids(session: Session) -> dict[str, int]:
    """返回三个 T13 冒烟用户的 id；缺失的当场建出来。"""
    found: dict[str, int] = {}
    for suffix, role in ROLES.items():
        username = _username(suffix)
        existing = session.scalar(select(User.id).where(User.username == username))
        if existing is not None:
            found[suffix] = int(existing)
            continue
        user = create_user(
            session,
            username=username,
            password_hash=hash_password(SMOKE_PASSWORD),
            display_name=f"T13 {suffix}",
            role_codes={role},
            data_scope=SCOPES[suffix],
        )
        found[suffix] = int(user.id)
    session.commit()
    return found


def _add_target(
    session: Session,
    *,
    suffix: str,
    channel: str,
    destination: str,
    enabled: bool = True,
    allow_private: bool = False,
    allow_confidential: bool = False,
) -> int:
    """建一条白名单条目（已存在就复用），并返回它的 id。"""
    name = _target_name(suffix)
    target = session.scalar(select(PushTarget).where(PushTarget.name == name))
    if target is None:
        target = PushTarget(
            name=name,
            channel=channel,
            destination=destination,
            description=f"T13 smoke {suffix}",
            enabled=enabled,
            allow_private=allow_private,
            allow_confidential=allow_confidential,
        )
        session.add(target)
        session.flush()
    session.commit()
    return int(target.id)


def _targets(session: Session) -> dict[str, int]:
    """一次建好 T13 的白名单，并按后缀返回每条的 id。"""
    found: dict[str, int] = {}
    for suffix, channel, destination, enabled, allow_private, allow_confidential in TARGET_SPECS:
        found[suffix] = _add_target(
            session,
            suffix=suffix,
            channel=channel,
            destination=destination,
            enabled=enabled,
            allow_private=allow_private,
            allow_confidential=allow_confidential,
        )
    return found


def _seed_reports(
    session: Session,
    ids: dict[str, int],
    actor_ids: dict[str, int],
) -> dict[str, int]:
    """先用真正的 Agent 加 CLI 生成一份报告，再停放三份同主题的兄弟报告。

    插入的这三份兄弟报告，替代的是已经走完发布流程的报告。这里直接写入，是因为本组
    检查针对的是*推送*守卫；它们需要的状态在 T11 冒烟里由真实流程产出，在这里再跑
    一遍只会让脚本更慢，并不会多覆盖什么。
    """
    agent_result = run_research_agent(
        session,
        topic_id=ids["topic_id"],
        settings=get_settings(),
        now=t8.SMOKE_NOW,
    )
    if agent_result.report_draft_id is None:
        raise RuntimeError("the Agent run did not create a report draft")

    generated = t11.run_cli("--agent-run-id", str(agent_result.agent_run_id))
    report_id = int(generated["report_id"])

    _refresh(session)
    report = session.get(Report, report_id)
    if report is None:
        raise RuntimeError("the report CLI reported a report that does not exist")
    report.visibility = "INTERNAL"
    report.sensitivity = "INTERNAL"
    report.publish_mode = "MANUAL"
    report.owner_id = actor_ids["editor"]
    session.flush()

    def sibling(
        title: str,
        *,
        status: str,
        visibility: str,
        sensitivity: str,
        published_at: datetime | None = None,
    ) -> Report:
        return Report(
            agent_run_id=report.agent_run_id,
            topic_id=report.topic_id,
            owner_id=actor_ids["editor"],
            title=title,
            current_version=1,
            content_markdown=f"# {title}\n",
            status=status,
            visibility=visibility,
            sensitivity=sensitivity,
            publish_mode="MANUAL",
            generated_at=t11.T11_NOW,
            published_at=published_at,
        )

    private = sibling(
        PRIVATE_TITLE,
        status="pending_review",
        visibility="PRIVATE",
        sensitivity="CONFIDENTIAL",
    )
    # 报告 C 刻意种成 `published` 而不是 `approved`。`publish_due_reports` 会合法地
    # 扫过*每一份* `approved` 报告，而这些兄弟报告既没有真实审批留下的 `passed`
    # 验证、也没有 `approve` 审核记录，于是那趟清扫会（正确地）把任何 `approved`
    # 兄弟报告终结为 `failed`。`published` 才是「已经走完发布流程」的诚实形态，
    # 也让它之后的投递检查始终指着一份可推送的报告。
    published = sibling(
        PUBLISHED_TITLE,
        status="published",
        visibility="INTERNAL",
        sensitivity="INTERNAL",
        published_at=t11.T11_NOW,
    )
    confidential = sibling(
        CONFIDENTIAL_TITLE,
        status="approved",
        visibility="INTERNAL",
        sensitivity="CONFIDENTIAL",
    )
    session.add_all([private, published, confidential])
    session.commit()
    return {
        "report_a": report_id,
        "report_b": int(private.id),
        "report_c": int(published.id),
        "report_d": int(confidential.id),
    }


# --------------------------------------------------------------------------------------
# 用全新会话读取状态
# --------------------------------------------------------------------------------------


def _report_state(factory: sessionmaker[Session], report_id: int) -> dict[str, Any]:
    """经一个全新会话读出一份报告的状态与版本。"""
    with factory() as session:
        report = session.get(Report, report_id)
        if report is None:
            raise RuntimeError(f"report {report_id} vanished")
        return {"id": report.id, "status": report.status, "version": report.current_version}


def _push_rows(factory: sessionmaker[Session], report_id: int) -> list[dict[str, Any]]:
    """把一份报告的投递轨迹读成普通值，按从旧到新排列。"""
    with factory() as session:
        rows = session.scalars(
            select(ReportPush).where(ReportPush.report_id == report_id).order_by(ReportPush.id)
        ).all()
        return [
            {
                "target_id": int(row.target_id),
                "channel": row.channel,
                "destination": row.destination,
                "status": row.status,
                "attempt": int(row.attempt),
                "error_code": row.error_code,
                "sent": row.sent_at is not None,
            }
            for row in rows
        ]


def _push_run_types(factory: sessionmaker[Session], report_id: int) -> list[str]:
    """读出一份报告的推送所创建的流水线运行的 `run_type` 值。"""
    with factory() as session:
        push_ids = select(ReportPush.pipeline_run_id).where(ReportPush.report_id == report_id)
        return sorted(
            {
                str(value)
                for value in session.scalars(
                    select(PipelineRun.run_type).where(PipelineRun.id.in_(push_ids))
                ).all()
            }
        )


def _push_audit(factory: sessionmaker[Session], report_id: int) -> list[dict[str, Any]]:
    """读出针对一份报告记录的 `report.push` 审计条目。"""
    with factory() as session:
        rows = session.scalars(
            select(AuditLog).where(
                AuditLog.action == "report.push",
                AuditLog.resource_id == str(report_id),
            )
        ).all()
        return [
            {
                "result": row.result,
                "replayed": (row.metadata_json or {}).get("replayed"),
                "auto": (row.metadata_json or {}).get("auto"),
                "error_code": (row.metadata_json or {}).get("error_code"),
            }
            for row in rows
        ]


def _plant_failed_row(
    session: Session,
    *,
    report_id: int,
    version: int,
    target_id: int,
    channel: str,
    destination: str,
    attempt: int,
    updated_at: datetime,
) -> int:
    """插入一条失败的投递行，替代一次被传输方拒掉的尝试。"""
    row = ReportPush(
        report_id=report_id,
        target_id=target_id,
        version=version,
        channel=channel,
        destination=destination,
        status="failed",
        attempt=attempt,
        idempotency_key=ReportPush.key_for(
            report_id=report_id,
            version=version,
            target_id=target_id,
        ),
        error_code="PUSH_DELIVERY_FAILED",
        error_message="offline:transient",
        created_at=updated_at,
        updated_at=updated_at,
    )
    session.add(row)
    session.commit()
    return int(row.id)


# --------------------------------------------------------------------------------------
# 判据核对
# --------------------------------------------------------------------------------------


def _status_guard_checks(
    report_a: int,
    report_b: int,
    targets: dict[str, int],
    reviewer_id: int,
) -> dict[str, Any]:
    """判据 1，走真正的 CLI：`draft` 与 `pending_review` 都不能被推送。"""
    draft_push = t11.run_cli_expecting_failure(
        "--report-id",
        str(report_a),
        "--action",
        "push",
        "--channel",
        "email",
        "--target-id",
        str(targets["email"]),
    )
    verified = t11.run_cli("--report-id", str(report_a), "--action", "verify")
    queued = t11.run_cli("--report-id", str(report_a), "--action", "publish")
    pending_push = t11.run_cli_expecting_failure(
        "--report-id",
        str(report_a),
        "--action",
        "push",
        "--channel",
        "email",
        "--target-id",
        str(targets["email"]),
    )
    approved = t11.run_cli(
        "--report-id",
        str(report_a),
        "--action",
        "review",
        "--decision",
        "approve",
        "--reviewer-id",
        str(reviewer_id),
    )
    # 报告 B 只是停在待审核，所以状态守卫必须先给出答案，才轮得到可见性守卫。
    report_b_push = t11.run_cli_expecting_failure(
        "--report-id",
        str(report_b),
        "--action",
        "push",
        "--channel",
        "email",
        "--target-id",
        str(targets["cleared"]),
    )
    return {
        "cli_verify_verdict": verified["verdict"],
        "cli_queue_status": queued["status"],
        "cli_approved_status": approved["status"],
        "cli_draft_push_code": draft_push.get("code"),
        "cli_pending_push_code": pending_push.get("code"),
        "report_b_pending_push_code": report_b_push.get("code"),
    }


def _allowlist_guard_checks(report_a: int, targets: dict[str, int]) -> dict[str, Any]:
    """判据 2，走真正的 CLI：只有渠道相符且启用的目标才可投递。"""
    delivered = t11.run_cli(
        "--report-id",
        str(report_a),
        "--action",
        "push",
        "--channel",
        "email",
        "--target-id",
        str(targets["email"]),
    )
    replay = t11.run_cli(
        "--report-id",
        str(report_a),
        "--action",
        "push",
        "--channel",
        "email",
        "--target-id",
        str(targets["email"]),
    )
    disabled = t11.run_cli_expecting_failure(
        "--report-id",
        str(report_a),
        "--action",
        "push",
        "--channel",
        "email",
        "--target-id",
        str(targets["disabled"]),
    )
    mismatched = t11.run_cli_expecting_failure(
        "--report-id",
        str(report_a),
        "--action",
        "push",
        "--channel",
        "webhook",
        "--target-id",
        str(targets["email"]),
    )
    unknown = t11.run_cli_expecting_failure(
        "--report-id",
        str(report_a),
        "--action",
        "push",
        "--channel",
        "email",
        "--target-id",
        "987654321",
    )
    return {
        "cli_first_push_status": delivered["status"],
        "cli_first_push_attempt": delivered["attempt"],
        "cli_first_push_delivered": delivered["delivered"],
        "cli_first_push_replayed": delivered["replayed"],
        "cli_replay_status": replay["status"],
        "cli_replay_attempt": replay["attempt"],
        "cli_replay_delivered": replay["delivered"],
        "cli_replay_replayed": replay["replayed"],
        "cli_replay_same_row": replay["push_id"] == delivered["push_id"],
        "cli_disabled_target_code": disabled.get("code"),
        "cli_mismatched_channel_code": mismatched.get("code"),
        "cli_unknown_target_code": unknown.get("code"),
    }


def _live_channel_check(session: Session, report_c: int, target_id: int) -> dict[str, Any]:
    """live 传输必须在写入任何行之前拒掉未配置的渠道。

    这是 live 路径上唯一能在不依赖可达设施的前提下核对的部分：配置错误的部署属于
    请求错误，而不是投递失败，因此它必须与可重试的传输问题保持可区分 —— 也不能留下
    一条推送行，让重试清扫反复在它上面绊倒。
    """
    settings = get_settings()
    live = settings.model_copy(
        update={"push_delivery": "live", "push_smtp_host": None, "push_email_from": None}
    )
    built_code = ""
    try:
        build_channel(live, "email")
    except PushTargetNotAllowed as exc:
        built_code = exc.code
    before = len(session.scalars(select(ReportPush).where(ReportPush.report_id == report_c)).all())
    refused_code = ""
    try:
        push_report(
            session,
            report_id=report_c,
            channel="email",
            target_id=target_id,
            settings=live,
            now=T13_NOW,
        )
    except PushTargetNotAllowed as exc:
        refused_code = exc.code
    _refresh(session)
    after = len(session.scalars(select(ReportPush).where(ReportPush.report_id == report_c)).all())
    return {
        "live_channel_configured": settings.push_delivery,
        "live_build_code": built_code,
        "live_push_code": refused_code,
        "live_rows_added": after - before,
    }


def _ui_checks(
    session: Session,
    client: TestClient,
    report_a: int,
    report_c: int,
    targets: dict[str, int],
) -> dict[str, Any]:
    """通过真正的管理界面驱动推送卡片，含界面附加的两道守卫。

    两个目的地是刻意在放行*之后*才加的：它们对报告 A 必须没有任何投递历史，这样
    面板推送成功才能证明界面确实发出了请求，而不是重放了早先那一行。
    """
    late_email = _add_target(
        session,
        suffix="late-email",
        channel="email",
        destination="t13-late@example.com",
    )
    late_webhook = _add_target(
        session,
        suffix="late-webhook",
        channel="webhook",
        destination="https://hooks.example.com/t13-late",
    )

    login = _login(client, "editor")
    if login.status_code != 303:
        raise RuntimeError(f"editor UI login failed: {login.status_code} {login.text}")

    panel = client.get(f"{UI}/{report_a}")
    history = client.get(f"{UI}/{report_c}")
    token = {"csrf_token": _csrf(client)}

    pushed = client.post(f"{UI}/{report_a}/push", data={**token, "target_id": late_email})
    replayed = client.post(f"{UI}/{report_a}/push", data={**token, "target_id": late_email})
    unknown = client.post(f"{UI}/{report_c}/push", data={**token, "target_id": "987654321"})
    # 渠道取自目标行，所以伪造的表单字段挑不了传输方式。
    forged = client.post(
        f"{UI}/{report_a}/push",
        data={**token, "target_id": late_webhook, "channel": "email"},
    )
    csrf_missing = client.post(f"{UI}/{report_a}/push", data={"target_id": late_email})
    viewer_login = _login(client, "viewer")
    viewer_push = client.post(
        f"{UI}/{report_c}/push",
        data={"csrf_token": _csrf(client), "target_id": targets["email"]},
    )

    return {
        "ui_session_cookie": bool(client.cookies.get(SESSION_COOKIE)),
        "ui_editor_login_status": login.status_code,
        "ui_panel_renders_push_card": f'action="{UI}/{report_a}/push"' in panel.text,
        "ui_panel_renders_history": "推送记录" in panel.text,
        "ui_panel_hint": "只有 approved / published 状态的报告可以推送" in history.text,
        "ui_push_flash": "已推送到 t13-late@example.com" in pushed.text,
        "ui_push_row_sent": 'class="ok-text">sent</td>' in pushed.text,
        "ui_replay_flash": "（该版本与目标此前已推送，未重复发送）" in replayed.text,
        "ui_unknown_target_code": "PUSH_TARGET_NOT_ALLOWED" in unknown.text,
        "ui_forged_channel_destination": (
            "已推送到 https://hooks.example.com/t13-late" in forged.text
        ),
        "ui_csrf_missing_status": csrf_missing.status_code,
        "ui_viewer_login_status": viewer_login.status_code,
        "ui_viewer_push_status": viewer_push.status_code,
        "ui_viewer_push_refused": "只读角色不能执行该操作" in viewer_push.text,
    }


def run_checks(
    session: Session,
    factory: sessionmaker[Session],
    client: TestClient,
    ids: dict[str, int],
    actor_ids: dict[str, int],
    targets: dict[str, int],
    reports: dict[str, int],
) -> dict[str, Any]:
    """让真正的 CLI、API 与界面走完整条 T13 验收路径。"""
    del ids
    report_a = reports["report_a"]
    report_b = reports["report_b"]
    report_c = reports["report_c"]
    report_d = reports["report_d"]

    status_guards = _status_guard_checks(report_a, report_b, targets, actor_ids["admin"])
    allowlist_guards = _allowlist_guard_checks(report_a, targets)

    admin = _bearer(client, "admin")
    editor = _bearer(client, "editor")
    viewer = _bearer(client, "viewer")

    # 白名单本身可读，渠道筛选还能把它收窄。
    target_list = client.get(f"{PUSH_API}/targets", headers=admin).json()["items"]
    webhook_only = client.get(
        f"{PUSH_API}/targets", params={"channel": "webhook"}, headers=admin
    ).json()["items"]
    viewer_targets = client.get(f"{PUSH_API}/targets", headers=viewer)

    # 判据 3：报告 B 先停在待审核，再被批准，然后因为目标未放行而被拒，最后投递到
    # 一个对 `PRIVATE` 与 `CONFIDENTIAL` 都放行的目标上。
    b_before = client.post(
        f"{API}/{report_b}/push",
        json={"channel": "email", "target_id": targets["cleared"]},
        headers=admin,
    )
    b_approved = t11.run_cli(
        "--report-id",
        str(report_b),
        "--action",
        "review",
        "--decision",
        "approve",
        "--reviewer-id",
        str(actor_ids["admin"]),
    )
    b_uncleared = client.post(
        f"{API}/{report_b}/push",
        json={"channel": "email", "target_id": targets["email"]},
        headers=admin,
    )
    b_delivered = client.post(
        f"{API}/{report_b}/push",
        json={"channel": "email", "target_id": targets["cleared"]},
        headers=admin,
    )

    # 一份 `CONFIDENTIAL` 报告，目标已放行但没有审批，仍然会被拒。
    d_guarded = client.post(
        f"{API}/{report_d}/push",
        json={"channel": "email", "target_id": targets["cleared"]},
        headers=admin,
    )

    # 判据 4 在 HTTP 层再验一遍，用一份没有其他推送挡路的报告。
    c_state = _report_state(factory, report_c)
    c_first = client.post(
        f"{API}/{report_c}/push",
        json={"channel": "email", "target_id": targets["email"]},
        headers=admin,
    )
    c_replay = client.post(
        f"{API}/{report_c}/push",
        json={"channel": "email", "target_id": targets["email"]},
        headers=admin,
    )
    viewer_push = client.post(
        f"{API}/{report_c}/push",
        json={"channel": "email", "target_id": targets["email"]},
        headers=viewer,
    )
    anonymous_push = client.post(
        f"{API}/{report_c}/push",
        json={"channel": "email", "target_id": targets["email"]},
    )

    live = _live_channel_check(session, report_c, targets["secondary"])
    c_run_types = _push_run_types(factory, report_c)

    # 判据 5 在 HTTP 层再验一遍：一条失败行被重投，一条不可用的行被跳过且不中断
    # 整批，一条已经耗尽预算的行则原样留着。
    version = c_state["version"]
    _plant_failed_row(
        session,
        report_id=report_c,
        version=version,
        target_id=targets["webhook"],
        channel="webhook",
        destination="https://hooks.example.com/t13",
        attempt=1,
        updated_at=T13_NOW,
    )
    _plant_failed_row(
        session,
        report_id=report_c,
        version=version,
        target_id=targets["disabled"],
        channel="email",
        destination="t13-disabled@example.com",
        attempt=1,
        updated_at=T13_NOW + timedelta(minutes=1),
    )
    _plant_failed_row(
        session,
        report_id=report_c,
        version=version,
        target_id=targets["cleared"],
        channel="email",
        destination="t13-cleared@example.com",
        attempt=3,
        updated_at=T13_NOW + timedelta(minutes=2),
    )
    failed_before = client.get(
        f"{PUSH_API}/records",
        params={"report_id": report_c, "status": "failed"},
        headers=admin,
    ).json()
    sweep = client.post(f"{PUSH_API}/retry", headers=admin).json()
    sweep_forbidden = client.post(f"{PUSH_API}/retry", headers=viewer)
    rows_after_sweep = _push_rows(factory, report_c)
    denied_retries = [
        entry for entry in _push_audit(factory, report_c) if entry["result"] == "denied"
    ]

    # CLI 清扫要有一条自己的行可捡，所以再种一次失败。
    _plant_failed_row(
        session,
        report_id=report_c,
        version=version,
        target_id=targets["secondary"],
        channel="email",
        destination="t13-secondary@example.com",
        attempt=1,
        updated_at=T13_NOW + timedelta(minutes=3),
    )
    cli_sweep = t11.run_cli("--action", "push-retry", "--limit", "50")

    # 判据 6：放行报告 A 会扇出到每个启用目标，全程无需人工步骤。
    audit_before_release = _push_audit(factory, report_a)
    rows_before_release = _push_rows(factory, report_a)
    scheduled = t11.run_cli("--report-id", str(report_a), "--action", "publish")
    released = t11.run_cli("--action", "publish-due")
    a_state = _report_state(factory, report_a)
    a_rows = _push_rows(factory, report_a)
    audit_after_release = _push_audit(factory, report_a)

    ui = _ui_checks(session, client, report_a, report_c, targets)

    settings = get_settings()
    return {
        "push_delivery": settings.push_delivery,
        "push_max_attempts": settings.push_max_attempts,
        "targets": targets,
        "enabled_target_count": len(target_list),
        "webhook_target_count": len(webhook_only),
        "viewer_targets_status": viewer_targets.status_code,
        "report_a": report_a,
        "report_b": report_b,
        "report_c": report_c,
        "report_d": report_d,
        **status_guards,
        **allowlist_guards,
        "report_b_blocked_code": b_before.json().get("code"),
        "report_b_approved_status": b_approved["status"],
        "report_b_uncleared_code": b_uncleared.json().get("code"),
        "report_b_delivered": b_delivered.json().get("delivered"),
        "report_b_rows": len(_push_rows(factory, report_b)),
        "report_d_code": d_guarded.json().get("code"),
        "report_d_rows": len(_push_rows(factory, report_d)),
        "api_first_push_status": c_first.json().get("status"),
        "api_first_push_attempt": c_first.json().get("attempt"),
        "api_first_push_delivered": c_first.json().get("delivered"),
        "api_first_push_replayed": c_first.json().get("replayed"),
        "api_replay_replayed": c_replay.json().get("replayed"),
        "api_replay_attempt": c_replay.json().get("attempt"),
        "api_replay_same_row": c_replay.json().get("push_id") == c_first.json().get("push_id"),
        "api_editor_push_status": client.post(
            f"{API}/{report_c}/push",
            json={"channel": "email", "target_id": targets["email"]},
            headers=editor,
        ).status_code,
        "viewer_push_status": viewer_push.status_code,
        "viewer_push_code": viewer_push.json().get("code"),
        "anonymous_push_status": anonymous_push.status_code,
        "viewer_retry_status": sweep_forbidden.status_code,
        "push_run_types": c_run_types,
        **live,
        "failed_records_before": failed_before["status_counts"].get("failed"),
        "sweep_attempted": sweep["attempted_count"],
        "sweep_delivered": sweep["delivered_count"],
        "sweep_failed": sweep["failed_count"],
        "sweep_replay_status": sweep["outcomes"][0]["status"] if sweep["outcomes"] else None,
        "sweep_replay_attempt": sweep["outcomes"][0]["attempt"] if sweep["outcomes"] else None,
        "sweep_replay_replayed": sweep["outcomes"][0]["replayed"] if sweep["outcomes"] else None,
        "rows_after_sweep": rows_after_sweep,
        "sweep_denied_codes": sorted(
            {entry["error_code"] for entry in denied_retries if entry["error_code"]}
        ),
        "cli_sweep_attempted": cli_sweep["attempted_count"],
        "cli_sweep_delivered": cli_sweep["delivered_count"],
        "release_scheduled_status": scheduled["status"],
        "release_count": len(
            [item for item in released["released"] if item["report_id"] == report_a]
        ),
        "report_a_status": a_state["status"],
        "report_a_rows_before_release": len(rows_before_release),
        "report_a_rows": len(a_rows),
        "report_a_channels": sorted({row["channel"] for row in a_rows}),
        "report_a_statuses": sorted({row["status"] for row in a_rows}),
        "report_a_attempts": sorted({row["attempt"] for row in a_rows}),
        "report_a_audit_before": len(audit_before_release),
        "report_a_audit_after": len(audit_after_release),
        "report_a_auto_pushes": len(audit_after_release) - len(audit_before_release),
        **ui,
    }


def _client(settings: Settings) -> TestClient:
    """在真正的应用与真正的数据库之上建一个客户端。"""
    return TestClient(create_app(settings))


def _csrf(client: TestClient) -> str:
    """返回浏览器此刻持有的 CSRF 值。"""
    token = client.cookies.get(CSRF_COOKIE)
    if not token:
        raise RuntimeError("the UI never issued a CSRF cookie")
    return token


def _login(client: TestClient, suffix: str) -> Any:
    """通过真正的登录表单登录一个冒烟用户。"""
    page = client.get("/ui/login", follow_redirects=False)
    if page.status_code != 200:
        raise RuntimeError(f"login page returned {page.status_code}")
    return client.post(
        "/ui/login",
        data={
            "csrf_token": _csrf(client),
            "username": _username(suffix),
            "password": SMOKE_PASSWORD,
            "next": UI,
        },
        follow_redirects=False,
    )


def _bearer(client: TestClient, suffix: str) -> dict[str, str]:
    """返回某个冒烟用户的 Bearer 请求头。"""
    response = client.post(
        "/api/v1/auth/login",
        json={"username": _username(suffix), "password": SMOKE_PASSWORD},
    )
    if response.status_code != 200:
        raise RuntimeError(f"API login for {suffix} failed: {response.status_code} {response.text}")
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _parse_args() -> argparse.Namespace:
    """解析验证脚本使用的可选清理模式。"""
    parser = argparse.ArgumentParser(description="Run T13 push smoke checks")
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Only remove push targets and T13 records and exit",
    )
    return parser.parse_args()


def main() -> None:
    """在真实 MySQL 上跑 T13 冒烟，并打印断言结果。"""
    args = _parse_args()
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    factory = create_session_factory(engine)

    actor_ids: dict[str, int] = {}
    payload: dict[str, Any] = {}
    try:
        with factory() as session:
            actor_ids = _actor_ids(session)
            _cleanup(session, actor_ids)

        if args.cleanup_only:
            print(
                json.dumps(
                    {"cleanup_only": True, "actors": sorted(actor_ids)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return

        with factory() as session:
            ids = t8.seed_topics(session)
            actor_ids = _actor_ids(session)
            targets = _targets(session)
            reports = _seed_reports(session, ids, actor_ids)
            with _client(settings) as client:
                payload = run_checks(
                    session,
                    factory,
                    client,
                    ids,
                    actor_ids,
                    targets,
                    reports,
                )
    finally:
        with factory() as session:
            _cleanup(session, actor_ids)
        engine.dispose()

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
