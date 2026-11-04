"""守护免登录分享链接：敏感度闸门、令牌摘要与过期。

真正承载安全性的性质不是「链接能用」，而是「只有被允许外流的报告才能拿到链接，
而且那一行无法被反推回链接」。这两点都直接对数据库断言，而不是看服务的返回值。
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from reports_support import AUTO_WITH_DELAY, SeededReport, build_report
from sqlalchemy import select
from sqlalchemy.orm import Session
from support import NOW, Workspace

from open_source_intelligence.persistence.models.reporting import Report, ReportShareLink
from open_source_intelligence.reports.sharing import (
    DEFAULT_SHARE_TTL_HOURS,
    MAX_SHARE_TTL_HOURS,
    ShareError,
    ShareLinkNotFound,
    ShareNotPublished,
    ShareSensitivityRefused,
    create_share_link,
    hash_share_token,
    list_share_links,
    resolve_share_token,
    revoke_share_link,
)


def seed_report(
    session: Session,
    workspace: Workspace,
    *,
    status: str = "published",
    sensitivity: str = "PUBLIC",
) -> Report:
    """按指定的发布状态与敏感度播种一份报告。

    这一行直接以终态构造出来，而不是一路迁移过去：这些用例关心的是分享闸门，
    若还去驱动发布策略，失败的原因就会和分享毫无关系。
    """
    case: SeededReport = build_report(session, workspace, publish_mode=AUTO_WITH_DELAY)
    report = Report(
        agent_run_id=case.agent_run_id,
        topic_id=case.topic.id,
        title="分享用周报",
        current_version=1,
        content_markdown="# 分享用周报\n\n正文。\n",
        status=status,
        visibility="INTERNAL",
        sensitivity=sensitivity,
        publish_mode="AUTO_WITH_DELAY",
        generated_at=NOW,
        published_at=NOW if status == "published" else None,
    )
    session.add(report)
    session.commit()
    return report


def test_a_public_published_report_gets_a_link(session: Session, workspace: Workspace) -> None:
    """顺利路径：拿回一个令牌，且它足够长，猜不出来。"""
    report = seed_report(session, workspace)

    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    assert created.url_path == f"/share/{created.token}"
    assert len(created.token) >= 43
    assert created.expires_at == NOW + timedelta(hours=DEFAULT_SHARE_TTL_HOURS)


def test_only_the_digest_is_stored(session: Session, workspace: Workspace) -> None:
    """存不下明文的数据库，也就泄不出明文。

    这张表长成这个样子正是为了这一点，所以断言直接落在那一行上：将来若有人
    「图方便」加一个明文列，这里就会失败。
    """
    report = seed_report(session, workspace)

    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    row = session.scalar(select(ReportShareLink).where(ReportShareLink.id == created.share_id))
    assert row is not None
    assert row.token_hash == hashlib.sha256(created.token.encode("utf-8")).hexdigest()
    assert row.token_hash == hash_share_token(created.token)
    assert created.token not in repr(row.__dict__)


def test_listing_never_returns_a_token(session: Session, workspace: Workspace) -> None:
    """列表载荷里没有任何字段可以承载明文。"""
    report = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    summaries = list_share_links(session, report_id=report.id)

    assert [summary.share_id for summary in summaries] == [created.share_id]
    payload = summaries[0].to_dict()
    assert "token" not in payload
    assert created.token not in str(payload)


@pytest.mark.parametrize("sensitivity", ["INTERNAL", "CONFIDENTIAL"])
def test_a_non_public_report_is_refused(
    session: Session, workspace: Workspace, sensitivity: str
) -> None:
    """闸门看的是敏感度，而不是可见性。

    这些报告的 `visibility` 一律是 INTERNAL —— 任何登录用户都能读，看上去
    「无害」，直到一条链接把它变成任何拿到 URL 的人都能读。
    """
    report = seed_report(session, workspace, sensitivity=sensitivity)

    with pytest.raises(ShareSensitivityRefused) as excinfo:
        create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    assert excinfo.value.code == "SHARE_SENSITIVITY_REFUSED"
    assert session.scalars(select(ReportShareLink)).all() == []


def test_a_public_report_that_is_not_published_is_refused(
    session: Session, workspace: Workspace
) -> None:
    """草稿不是组织已经认可的对外表述。"""
    report = seed_report(session, workspace, status="draft", sensitivity="PUBLIC")

    with pytest.raises(ShareNotPublished) as excinfo:
        create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    assert excinfo.value.code == "SHARE_NOT_PUBLISHED"
    assert session.scalars(select(ReportShareLink)).all() == []


def test_a_valid_token_resolves_and_counts_the_visit(
    session: Session, workspace: Workspace
) -> None:
    """解析会返回报告并记下访问次数，但不去改动令牌本身。"""
    report = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    resolved = resolve_share_token(session, created.token, now=NOW)

    assert resolved is not None
    assert resolved.report.id == report.id
    row = session.get(ReportShareLink, created.share_id)
    assert row is not None
    assert row.access_count == 1
    assert row.last_accessed_at is not None


def test_an_unknown_token_resolves_to_nothing(session: Session, workspace: Workspace) -> None:
    """没有行就没有答案，路由把每个 `None` 都变成一个 404。"""
    report = seed_report(session, workspace)
    create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    assert resolve_share_token(session, "not-a-real-token", now=NOW) is None


def test_a_revoked_link_stops_resolving(session: Session, workspace: Workspace) -> None:
    """吊销立即生效：下一次读取就失败，没有缓存需要等。"""
    report = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    revoke_share_link(
        session, report_id=report.id, share_id=created.share_id, actor_id=None, now=NOW
    )

    assert resolve_share_token(session, created.token, now=NOW) is None


def test_revoking_twice_is_idempotent(session: Session, workspace: Workspace) -> None:
    """意图（「确保它失效」）已经满足，因此不该报错。"""
    report = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)
    first = revoke_share_link(
        session, report_id=report.id, share_id=created.share_id, actor_id=None, now=NOW
    )

    second = revoke_share_link(
        session,
        report_id=report.id,
        share_id=created.share_id,
        actor_id=None,
        now=NOW + timedelta(hours=1),
    )

    assert first is True
    assert second is False
    row = session.get(ReportShareLink, created.share_id)
    assert row is not None
    assert row.revoked_at == NOW


def test_revoking_a_link_of_another_report_is_not_found(
    session: Session, workspace: Workspace
) -> None:
    """分享 id 限定在它所属的报告内，猜到的 id 无法经由另一份报告吊销。"""
    report = seed_report(session, workspace)
    other = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    with pytest.raises(ShareLinkNotFound):
        revoke_share_link(
            session, report_id=other.id, share_id=created.share_id, actor_id=None, now=NOW
        )


def test_an_expired_link_stops_resolving(session: Session, workspace: Workspace) -> None:
    """时钟是参数，所以测「过期」不必真的等到过期。"""
    report = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, ttl_hours=1, now=NOW)

    assert resolve_share_token(session, created.token, now=NOW + timedelta(minutes=30)) is not None
    assert resolve_share_token(session, created.token, now=NOW + timedelta(hours=2)) is None


def test_a_link_with_no_expiry_never_expires(session: Session, workspace: Workspace) -> None:
    """`ttl_hours=None` 是明确的「不过期」，不是漏填。"""
    report = seed_report(session, workspace)
    created = create_share_link(
        session, report_id=report.id, actor_id=None, ttl_hours=None, now=NOW
    )

    assert created.expires_at is None
    assert resolve_share_token(session, created.token, now=NOW + timedelta(days=3650)) is not None


def test_a_report_that_stops_being_public_kills_its_link(
    session: Session, workspace: Workspace
) -> None:
    """内容被重新定级，在下一次读取时生效，而不是在下一条链接上生效。

    这正是报告侧的条件要在解析时重新检查、而不是在签发链接时冻结下来的原因。
    """
    report = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    report.sensitivity = "INTERNAL"
    session.commit()

    assert resolve_share_token(session, created.token, now=NOW) is None


def test_a_retracted_report_kills_its_link(session: Session, workspace: Workspace) -> None:
    """撤回一份报告，也要同时撤回指向它的那个地址。"""
    report = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)

    report.status = "draft"
    session.commit()

    assert resolve_share_token(session, created.token, now=NOW) is None


@pytest.mark.parametrize("ttl_hours", [0, -1, MAX_SHARE_TTL_HOURS + 1])
def test_a_lifetime_outside_the_bounds_is_refused(
    session: Session, workspace: Workspace, ttl_hours: int
) -> None:
    """非法的有效期是调用方的错误，不能因此落一行数据。"""
    report = seed_report(session, workspace)

    with pytest.raises(ShareError) as excinfo:
        create_share_link(session, report_id=report.id, actor_id=None, ttl_hours=ttl_hours, now=NOW)

    assert excinfo.value.code == "SHARE_ERROR"
    assert session.scalars(select(ReportShareLink)).all() == []


def test_minting_and_revoking_are_audited(session: Session, workspace: Workspace) -> None:
    """日后唯一能追责的是操作者的动作，所以要记下来。"""
    from open_source_intelligence.persistence.models.audit import AuditLog

    report = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)
    revoke_share_link(
        session, report_id=report.id, share_id=created.share_id, actor_id=None, now=NOW
    )

    actions = session.scalars(
        select(AuditLog.action).where(AuditLog.resource_id == str(report.id))
    ).all()
    assert "report.share_create" in actions
    assert "report.share_revoke" in actions


def test_reading_a_share_is_not_audited(session: Session, workspace: Workspace) -> None:
    """匿名读取没有主体可归属，因此不能把审计表刷爆。"""
    from open_source_intelligence.persistence.models.audit import AuditLog

    report = seed_report(session, workspace)
    created = create_share_link(session, report_id=report.id, actor_id=None, now=NOW)
    before = len(session.scalars(select(AuditLog)).all())

    resolve_share_token(session, created.token, now=NOW)
    resolve_share_token(session, created.token, now=NOW)

    after = len(session.scalars(select(AuditLog)).all())
    assert after == before
