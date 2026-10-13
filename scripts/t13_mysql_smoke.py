"""Run T13 email/Webhook push smoke checks against MySQL.

The script reuses the T8 seeding helpers and the T11 CLI runner, then drives the *real* report
CLI, the *real* FastAPI application, and the *real* Jinja2/HTMX admin UI on MySQL rather than
in-memory SQLite. It exercises the T13 acceptance criteria end to end:

1. **Only an approved or published report may be pushed** — `draft` and `pending_review` are
   both refused, each with the same stable error code;
2. **Only an allowlisted destination receives anything** — an unknown target, a disabled one,
   and a channel that disagrees with the target row are all refused;
3. **Sensitive material needs an approval** — a `PRIVATE`/`CONFIDENTIAL` report is refused until
   a human approves it *and* the target was explicitly cleared for both; a `CONFIDENTIAL` report
   with a cleared target but no approval is still refused;
4. **Delivery is idempotent and recorded** — the same (report, version, target) triple is
   delivered once, replays on a repeat request, and leaves a trail;
5. **A failed delivery is retryable** — the API sweep and the CLI sweep each redeliver a failed
   row on the same row, advance its attempt count, skip an unusable row without stopping the
   batch, and leave a row that spent its budget alone;
6. **A release fans out automatically** — publishing pushes to every enabled target with no
   manual step, and the report still ends up published.

The default `push_delivery="offline"` transport is used throughout. That is deliberate: it is
the deployment default, and it keeps the payload deterministic without reachable SMTP or HTTP
infrastructure. The `live` transport is covered at its configuration boundary instead — an
unconfigured live channel must be refused *before* a delivery row is written, because a
misconfigured deployment is a request error rather than a retryable transport failure.

Every database read the script asserts on goes through a *fresh* session, so a passing
assertion cannot be explained by the identity map of the session that wrote the row.
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

#: `(suffix, channel, destination, enabled, allow_private, allow_confidential)`.
#: The two `allow_*` switches are off for every target except the explicitly cleared one, which
#: is what makes the "cleared target" and "uncleared target" assertions meaningful.
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
    """Return the smoke target name for one entry of the allowlist."""
    return f"{TARGET_PREFIX} {suffix}__"


def _username(suffix: str) -> str:
    """Return the smoke username for one role."""
    return f"{ACTOR_PREFIX}{suffix}__"


def _refresh(session: Session) -> None:
    """Drop cached rows after the CLI wrote from another process.

    `rollback()` alone leaves already-loaded instances holding their old attribute values, so a
    later query can return a row whose ORM state no longer matches the SQL predicate.
    Production runs one short-lived session per task and is not exposed to this.
    """
    session.rollback()
    session.expire_all()


# --------------------------------------------------------------------------------------
# Seeding and cleanup
# --------------------------------------------------------------------------------------


def _t13_report_ids(session: Session, actor_ids: dict[str, int]) -> list[int]:
    """Return the reports a previous or current T13 run owns."""
    ids = list(actor_ids.values())
    if not ids:
        return []
    return [
        int(value) for value in session.scalars(select(Report.id).where(Report.owner_id.in_(ids)))
    ]


def _cleanup(session: Session, actor_ids: dict[str, int]) -> None:
    """Remove every row a T13 smoke run created, then the T8 records underneath.

    The order matters: `report_pushes.target_id` is `ON DELETE RESTRICT`, so the delivery rows
    have to go before the allowlist entries they point at.
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
    session.execute(delete(PushTarget).where(PushTarget.name.like(f"{TARGET_PREFIX}%")))
    ids = list(actor_ids.values())
    if ids:
        session.execute(delete(UserRole).where(UserRole.user_id.in_(ids)))
        session.execute(delete(User).where(User.id.in_(ids)))
    session.commit()
    t8.cleanup_verification_records(session)


def _actor_ids(session: Session) -> dict[str, int]:
    """Return the ids of the three T13 smoke users, creating them when missing."""
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
    """Create one allowlist entry (or return the existing one) and return its id."""
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
    """Create the T13 allowlist in one pass and return each entry's id by suffix."""
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
    """Generate one report through the real Agent plus CLI, then park three siblings.

    The three inserted siblings stand in for reports that already travelled the publishing
    workflow. They are written directly because these checks are about the *push* guards; the
    states they need are produced by the real workflow in the T11 smoke, and re-driving it here
    would only make this script slower without covering anything new.
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
    # Report C is seeded `published` rather than `approved` on purpose. `publish_due_reports`
    # legitimately sweeps *every* `approved` report, and these siblings carry neither the
    # `passed` verification nor the `approve` review that a real approval leaves behind, so the
    # sweep would (correctly) terminate any `approved` sibling as `failed`. A `published` report
    # is the honest shape for "already travelled the publishing workflow", and it keeps the
    # delivery checks that run after the release pointed at a pushable report.
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
# Reading state through fresh sessions
# --------------------------------------------------------------------------------------


def _report_state(factory: sessionmaker[Session], report_id: int) -> dict[str, Any]:
    """Read one report's status and version through a fresh session."""
    with factory() as session:
        report = session.get(Report, report_id)
        if report is None:
            raise RuntimeError(f"report {report_id} vanished")
        return {"id": report.id, "status": report.status, "version": report.current_version}


def _push_rows(factory: sessionmaker[Session], report_id: int) -> list[dict[str, Any]]:
    """Read one report's delivery trail as plain values, oldest first."""
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
    """Read the `run_type` values of the pipeline runs one report's pushes created."""
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
    """Read the `report.push` audit entries recorded against one report."""
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
    """Insert a failed delivery row, standing in for a transport that refused the last try."""
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
# Criterion checks
# --------------------------------------------------------------------------------------


def _status_guard_checks(
    report_a: int,
    report_b: int,
    targets: dict[str, int],
    reviewer_id: int,
) -> dict[str, Any]:
    """Criterion 1 through the real CLI: `draft` and `pending_review` cannot be pushed."""
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
    # Report B is only parked for review, so the status guard has to answer before the
    # visibility guard can be reached at all.
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
    """Criterion 2 through the real CLI: only a matching, enabled target is deliverable."""
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
    """The live transport must refuse an unconfigured channel before a row is written.

    This is the one part of the live path that can be checked without reachable
    infrastructure: a misconfigured deployment is a request error, not a delivery failure, so
    it has to stay distinguishable from a retryable transport problem — and it must not leave a
    push row behind for the retry sweep to keep tripping over.
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
    """Drive the push card through the real admin UI, including its two extra guards.

    The two destinations are added *after* the release on purpose: they must have no delivery
    history for report A, so a successful panel push proves the UI actually sends rather than
    replaying an earlier row.
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
    # The channel is taken from the target row, so a forged form field cannot pick a transport.
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
    """Drive the real CLI, API, and UI through the whole T13 acceptance path."""
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

    # The allowlist itself is readable, and the channel filter narrows it.
    target_list = client.get(f"{PUSH_API}/targets", headers=admin).json()["items"]
    webhook_only = client.get(
        f"{PUSH_API}/targets", params={"channel": "webhook"}, headers=admin
    ).json()["items"]
    viewer_targets = client.get(f"{PUSH_API}/targets", headers=viewer)

    # Criterion 3: report B is parked for review, then approved, then refused for an uncleared
    # target, then delivered to one that was cleared for both `PRIVATE` and `CONFIDENTIAL`.
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

    # A `CONFIDENTIAL` report with a cleared target but no approval is still refused.
    d_guarded = client.post(
        f"{API}/{report_d}/push",
        json={"channel": "email", "target_id": targets["cleared"]},
        headers=admin,
    )

    # Criterion 4 on the HTTP surface, on a report with no other pushes in the way.
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

    # Criterion 5 on the HTTP surface: one failed row is redelivered, an unusable one is skipped
    # without stopping the batch, and one that spent its budget is left alone.
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

    # The CLI sweep needs a row of its own to pick up, so one more failure is planted.
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

    # Criterion 6: releasing report A fans out to every enabled target with no manual step.
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
    """Build a client over the real application and the real database."""
    return TestClient(create_app(settings))


def _csrf(client: TestClient) -> str:
    """Return the CSRF value the browser currently holds."""
    token = client.cookies.get(CSRF_COOKIE)
    if not token:
        raise RuntimeError("the UI never issued a CSRF cookie")
    return token


def _login(client: TestClient, suffix: str) -> Any:
    """Sign one smoke user in through the real login form."""
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
    """Return the bearer header for one smoke user."""
    response = client.post(
        "/api/v1/auth/login",
        json={"username": _username(suffix), "password": SMOKE_PASSWORD},
    )
    if response.status_code != 200:
        raise RuntimeError(f"API login for {suffix} failed: {response.status_code} {response.text}")
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _parse_args() -> argparse.Namespace:
    """Parse the optional cleanup mode used by the verification script."""
    parser = argparse.ArgumentParser(description="Run T13 push smoke checks")
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Only remove push targets and T13 records and exit",
    )
    return parser.parse_args()


def main() -> None:
    """Run the T13 smoke checks on real MySQL and print the assertion payload."""
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
