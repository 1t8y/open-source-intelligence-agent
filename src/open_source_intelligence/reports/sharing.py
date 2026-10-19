"""Bearer links that make one published PUBLIC report readable without a login.

The gate is `sensitivity`, not `visibility`. The two vocabularies answer different questions:
`visibility` says who inside the system may read a report, `sensitivity` says whether the
content may leave the organisation. A share link is an address with no login behind it, so it
is the second question that has to be asked. Gating on `visibility` would have been the
natural mistake - `INTERNAL` means "every authenticated user", which reads as "safe to hand
out" right up until a link makes it readable by everyone with the URL.

Only `sha256(token)` is stored. The plaintext exists in exactly one place: the return value of
`create_share_link`, which the caller renders once. This is what keeps a table dump from being
a set of working credentials.
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

#: The only sensitivity that may leave the building through a link.
SHAREABLE_SENSITIVITY = "PUBLIC"

#: Only published reports may be shared. A draft or a report under review is not a statement
#: the organisation has agreed to, and a link cannot be recalled from a reader's browser cache
#: the way an internal page can be taken down.
SHAREABLE_STATUS = "published"

#: 32 bytes -> 43 urlsafe characters. Enumerable only by guessing 2^256 values.
SHARE_TOKEN_BYTES = 32

SHARE_PATH_PREFIX = "/share/"

#: A week. Long enough to be useful in a weekly-report workflow, short enough that a link
#: forwarded into a group chat stops working on its own.
DEFAULT_SHARE_TTL_HOURS = 168
MAX_SHARE_TTL_HOURS = 8760


class ShareError(ValueError):
    """Base class for share-link failures carrying a stable `code`."""

    code = "SHARE_ERROR"


class ShareLinkNotFound(ShareError):
    """Raised when the addressed share link (or its report) does not exist."""

    code = "NOT_FOUND"


class ShareSensitivityRefused(ShareError):
    """Raised when a report whose sensitivity is not `PUBLIC` is offered for sharing.

    This is the guard that keeps a new public door from opening onto internal content.
    """

    code = "SHARE_SENSITIVITY_REFUSED"


class ShareNotPublished(ShareError):
    """Raised when a `PUBLIC` report has not reached `published` yet."""

    code = "SHARE_NOT_PUBLISHED"


@dataclass(frozen=True, slots=True)
class ShareLinkCreated:
    """The one and only time a plaintext token leaves this module."""

    share_id: int
    token: str
    url_path: str
    expires_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready payload including the plaintext token."""
        return {
            "share_id": self.share_id,
            "token": self.token,
            "url_path": self.url_path,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


@dataclass(frozen=True, slots=True)
class ShareLinkSummary:
    """Listable view of a share link. Deliberately has no token field."""

    share_id: int
    created_by: int | None
    expires_at: datetime | None
    revoked_at: datetime | None
    last_accessed_at: datetime | None
    access_count: int
    created_at: datetime

    @property
    def revoked(self) -> bool:
        """Return whether the link has been explicitly revoked."""
        return self.revoked_at is not None

    def is_active(self, now: datetime | None = None) -> bool:
        """Return whether the link would resolve right now (report-side checks excluded)."""
        if self.revoked_at is not None:
            return False
        if self.expires_at is None:
            return True
        return self.expires_at > (now or datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready payload. Never includes a token, by construction."""
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
    """A report resolved through a live share token."""

    report: Report
    share_id: int


def hash_share_token(token: str) -> str:
    """Return the stored digest of a plaintext token.

    The single definition of "what a token looks like in the database". Both the writer and
    the reader call this, so they cannot drift into agreeing on a format nobody else has.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalize_ttl(ttl_hours: int | None) -> int | None:
    """Validate a requested lifetime, treating `None` as "no expiry"."""
    if ttl_hours is None:
        return None
    if not 1 <= ttl_hours <= MAX_SHARE_TTL_HOURS:
        raise ShareError(f"ttl_hours must be between 1 and {MAX_SHARE_TTL_HOURS}")
    return ttl_hours


def _require_shareable(report: Report) -> None:
    """Refuse a report whose sensitivity or status disqualifies it from a public address."""
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
    """Mint a bearer link for one published `PUBLIC` report.

    Raises:
        ShareLinkNotFound: the report does not exist.
        ShareSensitivityRefused: the report's sensitivity is not `PUBLIC`.
        ShareNotPublished: the report is `PUBLIC` but not published.
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
    """Resolve a plaintext token to its report, or `None` for every kind of miss.

    "Every kind" is the point. A wrong token, a revoked link, an expired link, a report that
    was retracted or is no longer `PUBLIC` - all return `None` so the route can answer one
    identical `404`. Distinguishing them would let a random-string holder ask "did this report
    ever exist", which is exactly the question an outsider should not be able to answer.
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

    # A successful read is the only thing this path writes. It is not audited: the reader has
    # no identity, so an audit row would record "somebody, anonymous, read something" once per
    # refresh and bury the operator actions that do carry an actor.
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
    """Revoke one link. Returns whether this call performed the revocation.

    Idempotent on purpose: revoking an already-revoked link returns `False` and changes
    nothing, because the caller's intent ("make sure it stops working") is already satisfied.
    A second revocation is not an error and not a write, so it is not audited either - the
    audit trail keeps the moment the link actually stopped working.
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
    """Return every link ever minted for a report, newest first, without plaintext tokens."""
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
