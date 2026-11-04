"""发放持有者链接，让一份已发布的 PUBLIC 报告无需登录即可阅读。

闸门看的是 `sensitivity`，不是 `visibility`。这两套词汇回答的是不同的问题：`visibility`
说明系统内部谁可以读这份报告，`sensitivity` 说明内容能否离开组织。分享链接是一个背后
没有登录的地址，所以要问的是第二个问题。按 `visibility` 设闸会是最自然的错误 ——
`INTERNAL` 的意思是「任何已认证用户」，读起来像「可以放心发出去」，直到一条链接让它
变得任何拿到 URL 的人都能读。

只存 `sha256(token)`。明文只出现在一个地方：`create_share_link` 的返回值，由调用方
渲染一次。正是这一点，让一份表导出不至于变成一组可用的凭据。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.reporting import Report, ReportShareLink
from open_source_intelligence.security.audit import record_audit_event

#: 唯一允许通过链接带出组织的敏感级别。
SHAREABLE_SENSITIVITY = "PUBLIC"

#: 只有已发布的报告才能分享。草稿或仍在评审的报告还不是组织认可过的说法，而一条链接
#: 无法像内部页面那样被下线，也从读者的浏览器缓存里收不回来。
SHAREABLE_STATUS = "published"

#: 32 字节 -> 43 个 URL 安全字符。只能靠猜 2^256 个值来枚举。
SHARE_TOKEN_BYTES = 32

SHARE_PATH_PREFIX = "/share/"

#: 一周。在周报工作流里够用，又短到被转发进群聊的链接会自己失效。
DEFAULT_SHARE_TTL_HOURS = 168
MAX_SHARE_TTL_HOURS = 8760


class ShareError(ValueError):
    """携带稳定 `code` 的分享链接失败基类。"""

    code = "SHARE_ERROR"


class ShareLinkNotFound(ShareError):
    """当指定的分享链接（或其报告）不存在时抛出。"""

    code = "NOT_FOUND"


class ShareSensitivityRefused(ShareError):
    """当一份敏感级别不是 `PUBLIC` 的报告被拿去分享时抛出。

    这道守卫的作用，是别让一扇新开的公开门通向内部内容。
    """

    code = "SHARE_SENSITIVITY_REFUSED"


class ShareNotPublished(ShareError):
    """当一份 `PUBLIC` 报告尚未到达 `published` 时抛出。"""

    code = "SHARE_NOT_PUBLISHED"


@dataclass(frozen=True, slots=True)
class ShareLinkCreated:
    """明文令牌离开本模块的唯一一次。"""

    share_id: int
    token: str
    url_path: str
    expires_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        """返回可转 JSON 的载荷，其中包含明文令牌。"""
        return {
            "share_id": self.share_id,
            "token": self.token,
            "url_path": self.url_path,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


@dataclass(frozen=True, slots=True)
class ShareLinkSummary:
    """分享链接的可列表视图。刻意不含令牌字段。"""

    share_id: int
    created_by: int | None
    expires_at: datetime | None
    revoked_at: datetime | None
    last_accessed_at: datetime | None
    access_count: int
    created_at: datetime

    @property
    def revoked(self) -> bool:
        """返回该链接是否已被显式撤销。"""
        return self.revoked_at is not None

    def is_active(self, now: datetime | None = None) -> bool:
        """返回该链接此刻是否还能解析（不含报告侧的检查）。"""
        if self.revoked_at is not None:
            return False
        if self.expires_at is None:
            return True
        return self.expires_at > (now or datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """返回可转 JSON 的载荷。构造上绝不包含令牌。"""
        return {
            "share_id": self.share_id,
            "created_by": self.created_by,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "last_accessed_at": (
                self.last_accessed_at.isoformat() if self.last_accessed_at else None
            ),
            "access_count": self.access_count,
            "created_at": self.created_at.isoformat(),
            "revoked": self.revoked,
            "active": self.is_active(),
        }


@dataclass(frozen=True, slots=True)
class SharedReport:
    """通过一个有效分享令牌解析出的报告。"""

    report: Report
    share_id: int


def hash_share_token(token: str) -> str:
    """返回明文令牌在库里存储的摘要。

    这是「令牌在数据库里长什么样」的唯一定义。写侧和读侧都调用它，两者就不可能各自
    漂移成一种没有第三个人认可的格式。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalize_ttl(ttl_hours: int | None) -> int | None:
    """校验请求的有效期，把 `None` 视为「不过期」。"""
    if ttl_hours is None:
        return None
    if not 1 <= ttl_hours <= MAX_SHARE_TTL_HOURS:
        raise ShareError(f"ttl_hours must be between 1 and {MAX_SHARE_TTL_HOURS}")
    return ttl_hours


def _require_shareable(report: Report) -> None:
    """拒绝敏感级别或状态不具备公开地址资格的报告。"""
    if report.sensitivity != SHAREABLE_SENSITIVITY:
        raise ShareSensitivityRefused(
            f"report {report.id} has sensitivity {report.sensitivity!r}; only "
            f"{SHAREABLE_SENSITIVITY!r} reports may be shared through a public link"
        )
    if report.status != SHAREABLE_STATUS:
        raise ShareNotPublished(
            f"report {report.id} is {report.status!r}; only {SHAREABLE_STATUS!r} reports may be "
            f"shared"
        )


def create_share_link(
    session: Session,
    *,
    report_id: int,
    actor_id: int | None,
    ttl_hours: int | None = DEFAULT_SHARE_TTL_HOURS,
    now: datetime | None = None,
) -> ShareLinkCreated:
    """为一份已发布的 `PUBLIC` 报告铸造一条持有者链接。

    抛出：
        ShareLinkNotFound：报告不存在。
        ShareSensitivityRefused：报告的敏感级别不是 `PUBLIC`。
        ShareNotPublished：报告是 `PUBLIC`，但尚未发布。
    """
    report = session.get(Report, report_id)
    if report is None:
        raise ShareLinkNotFound(f"report {report_id} does not exist")
    _require_shareable(report)

    resolved_ttl = _normalize_ttl(ttl_hours)
    moment = now or datetime.now(UTC)
    expires_at = moment + timedelta(hours=resolved_ttl) if resolved_ttl is not None else None

    token = secrets.token_urlsafe(SHARE_TOKEN_BYTES)
    link = ReportShareLink(
        report_id=report.id,
        token_hash=hash_share_token(token),
        created_by=actor_id,
        expires_at=expires_at,
        access_count=0,
    )
    session.add(link)
    session.flush()
    record_audit_event(
        session,
        action="report.share_create",
        resource_type="report",
        resource_id=str(report.id),
        result="success",
        actor_id=actor_id,
        metadata_json={
            "share_id": link.id,
            "expires_at": expires_at.isoformat() if expires_at else None,
        },
    )
    session.commit()
    return ShareLinkCreated(
        share_id=link.id,
        token=token,
        url_path=f"{SHARE_PATH_PREFIX}{token}",
        expires_at=expires_at,
    )


def resolve_share_token(
    session: Session,
    token: str,
    *,
    now: datetime | None = None,
) -> SharedReport | None:
    """把明文令牌解析成对应报告；任何一种落空都返回 `None`。

    「任何一种」正是重点。错误的令牌、已撤销的链接、已过期的链接、被撤回或不再是
    `PUBLIC` 的报告 —— 一律返回 `None`，路由才能给出同一个 `404`。若把它们区分开，
    一个拿着随机字符串的人就能反问「这份报告到底存不存在」，而这恰恰是外部人不该能
    回答的问题。
    """
    if not token:
        return None
    link = session.scalar(
        select(ReportShareLink).where(ReportShareLink.token_hash == hash_share_token(token))
    )
    if link is None:
        return None
    moment = now or datetime.now(UTC)
    if link.revoked_at is not None:
        return None
    if link.expires_at is not None and link.expires_at <= moment:
        return None
    report = session.get(Report, link.report_id)
    if report is None or report.status != SHAREABLE_STATUS:
        return None
    if report.sensitivity != SHAREABLE_SENSITIVITY:
        return None

    # 一次成功的读取是这条路径唯一的写入。它不进审计：读者没有身份，审计行只会每刷新
    # 一次就记一条「某人、匿名、读了点什么」，把真正带操作者的运维动作埋掉。
    link.access_count += 1
    link.last_accessed_at = moment
    session.commit()
    return SharedReport(report=report, share_id=link.id)


def revoke_share_link(
    session: Session,
    *,
    report_id: int,
    share_id: int,
    actor_id: int | None,
    now: datetime | None = None,
) -> bool:
    """撤销一条链接。返回本次调用是否真的执行了撤销。

    刻意做成幂等：撤销一条已撤销的链接返回 `False` 且不做任何改动，因为调用方的意图
    （「确保它不再工作」）已经满足了。第二次撤销既不是错误也不是写入，所以同样不进
    审计 —— 审计轨迹保留的是链接真正失效的那一刻。
    """
    link = session.scalar(
        select(ReportShareLink).where(
            ReportShareLink.id == share_id,
            ReportShareLink.report_id == report_id,
        )
    )
    if link is None:
        raise ShareLinkNotFound(f"share link {share_id} does not exist for report {report_id}")
    if link.revoked_at is not None:
        return False

    link.revoked_at = now or datetime.now(UTC)
    record_audit_event(
        session,
        action="report.share_revoke",
        resource_type="report",
        resource_id=str(report_id),
        result="success",
        actor_id=actor_id,
        metadata_json={"share_id": share_id},
    )
    session.commit()
    return True


def list_share_links(session: Session, *, report_id: int) -> list[ShareLinkSummary]:
    """返回某份报告历史上铸造过的所有链接，最新的在前，不含明文令牌。"""
    rows = session.scalars(
        select(ReportShareLink)
        .where(ReportShareLink.report_id == report_id)
        .order_by(ReportShareLink.id.desc())
    ).all()
    return [
        ShareLinkSummary(
            share_id=row.id,
            created_by=row.created_by,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
            last_accessed_at=row.last_accessed_at,
            access_count=row.access_count,
            created_at=row.created_at,
        )
        for row in rows
    ]
