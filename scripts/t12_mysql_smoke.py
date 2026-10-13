"""Run T12 report API and admin UI smoke checks against MySQL.

The script reuses the T8 seeding helpers and the T11 CLI runner, then drives the *real*
FastAPI application — the JSON report API and the Jinja2/HTMX admin UI — against MySQL rather
than in-memory SQLite. It exercises the T12 acceptance path end to end:

1. **View** — the list is filtered by the caller's visibility scope in the database, and the
   detail page carries the verdict, the citations, and the version history;
2. **Edit** — a content edit appends an immutable version, copies the citation set, and parks
   the report for re-verification;
3. **Review** — `MANUAL` parks the report, `request_changes` sends it back, and an approval is
   what finally unlocks the release;
4. **Publish** — the approved report is released through the same policy engine the CLI uses;
5. **Roll back** — a historical version is restored as a *new* version rather than a rewind.

Two guards the UI adds on top of the API are checked as well: the cookie session (including
the double-submit CSRF token) and the read-only role.

`TestClient` runs the real ASGI application in process, so the real routers, dependencies,
middleware, templates, and static files are exercised. Every database read the script asserts
on goes through a *fresh* short-lived session, so a passing assertion cannot be explained by
the identity map of the session that wrote the row.
"""

import argparse
import json
import re
from typing import Any

import t8_mysql_smoke as t8
import t11_mysql_smoke as t11
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from open_source_intelligence.agent.runtime import run_research_agent
from open_source_intelligence.api.app import create_app
from open_source_intelligence.core.config import Settings, get_settings
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.identity import User, UserRole
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
UI = "/ui/reports"
SMOKE_PASSWORD = "t12-smoke-password-not-for-production"
ACTOR_PREFIX = "__t12_"
PRIVATE_TITLE = "__T12 private sibling__"

#: Appended by the UI edit form. Deliberately digit-free: a section that states a number must
#: cite a metric snapshot, so this note must not turn a clean report into a
#: `NUMBER_WITHOUT_METRIC` failure.
EDIT_NOTE = "\n\n编辑说明：人工复核了来源链接与数字口径，未做其他改动。\n"
SECOND_EDIT_NOTE = "\n\n补充说明：趋势对比已纳入正文复核范围。\n"

ROLES = {"admin": "ADMIN", "editor": "EDITOR", "viewer": "VIEWER"}
SCOPES = {"admin": "personal", "editor": "team", "viewer": "personal"}


def _username(suffix: str) -> str:
    """Return the smoke username for one role."""
    return f"{ACTOR_PREFIX}{suffix}__"


def _cleanup(session: Session, actor_ids: dict[str, int]) -> None:
    """Remove every row a previous T12 smoke run created, then the T8 records underneath."""
    ids = list(actor_ids.values())
    if ids:
        report_ids = list(session.scalars(select(Report.id).where(Report.owner_id.in_(ids))).all())
        if report_ids:
            session.execute(delete(ReportReview).where(ReportReview.report_id.in_(report_ids)))
            session.execute(
                delete(ReportVerification).where(ReportVerification.report_id.in_(report_ids))
            )
            session.execute(delete(ReportSource).where(ReportSource.report_id.in_(report_ids)))
            session.execute(delete(ReportVersion).where(ReportVersion.report_id.in_(report_ids)))
            session.execute(
                delete(AuditLog).where(
                    AuditLog.resource_id.in_([str(value) for value in report_ids])
                )
            )
            session.execute(delete(Report).where(Report.id.in_(report_ids)))
        session.execute(delete(UserRole).where(UserRole.user_id.in_(ids)))
        session.execute(delete(User).where(User.id.in_(ids)))
        session.commit()
    t8.cleanup_verification_records(session)


def _actor_ids(session: Session) -> dict[str, int]:
    """Return the ids of the three T12 smoke users, creating them when missing."""
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
            display_name=f"T12 {suffix}",
            role_codes={role},
            data_scope=SCOPES[suffix],
        )
        found[suffix] = int(user.id)
    session.commit()
    return found


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


def _snapshot(factory: sessionmaker[Session], report_id: int) -> dict[str, Any]:
    """Read one report's state through a fresh session and flatten it to plain values."""
    with factory() as session:
        report = session.get(Report, report_id)
        if report is None:
            raise RuntimeError(f"report {report_id} vanished")
        versions = session.scalars(
            select(ReportVersion)
            .where(ReportVersion.report_id == report_id)
            .order_by(ReportVersion.version)
        ).all()
        source_versions = sorted(
            {
                int(value)
                for value in session.scalars(
                    select(ReportSource.version).where(ReportSource.report_id == report_id)
                ).all()
            }
        )
        return {
            "id": report.id,
            "title": report.title,
            "status": report.status,
            "version": report.current_version,
            "visibility": report.visibility,
            "sensitivity": report.sensitivity,
            "publish_mode": report.publish_mode,
            "owner_id": report.owner_id,
            "content": report.content_markdown,
            "versions": [row.version for row in versions],
            "change_reasons": [row.change_reason or "" for row in versions],
            "source_versions": source_versions,
        }


def _verdict(factory: sessionmaker[Session], report_id: int, version: int) -> str | None:
    """Return the stored verdict for one report version."""
    with factory() as session:
        row = session.scalar(
            select(ReportVerification).where(
                ReportVerification.report_id == report_id,
                ReportVerification.version == version,
            )
        )
        return None if row is None else row.verdict


def _rows(factory: sessionmaker[Session], entity: Any, report_id: int) -> int:
    """Return how many rows of one report-scoped table exist for a report."""
    with factory() as session:
        return len(session.scalars(select(entity).where(entity.report_id == report_id)).all())


def _audit_actions(factory: sessionmaker[Session], report_id: int) -> list[str]:
    """Return the distinct report actions recorded against one report."""
    with factory() as session:
        rows = session.scalars(
            select(AuditLog.action).where(
                AuditLog.resource_type == "report",
                AuditLog.resource_id == str(report_id),
            )
        ).all()
        return sorted(set(rows))


def _api_list_ids(client: TestClient, headers: dict[str, str]) -> list[int]:
    """Return every report id the caller may read, walking the paginated list."""
    collected: list[int] = []
    page = 1
    while True:
        response = client.get(f"{API}?page={page}&page_size=100", headers=headers)
        if response.status_code != 200:
            raise RuntimeError(f"report list failed: {response.status_code} {response.text}")
        body = response.json()
        collected.extend(int(item["id"]) for item in body["items"])
        if page * body["page_size"] >= body["total"]:
            return collected
        page += 1


def _seed_report(
    session: Session,
    ids: dict[str, int],
    actor_ids: dict[str, int],
) -> tuple[int, int]:
    """Generate a report through the real Agent plus CLI, then add a private sibling."""
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

    report = session.get(Report, report_id)
    if report is None:
        raise RuntimeError("the report CLI reported a report that does not exist")
    report.visibility = "INTERNAL"
    report.sensitivity = "INTERNAL"
    report.owner_id = actor_ids["editor"]
    session.flush()

    sibling = Report(
        agent_run_id=report.agent_run_id,
        topic_id=report.topic_id,
        owner_id=actor_ids["editor"],
        title=PRIVATE_TITLE,
        current_version=1,
        content_markdown="# 私有周报\n",
        status="draft",
        visibility="PRIVATE",
        sensitivity="INTERNAL",
        publish_mode="AUTO_WITH_DELAY",
        generated_at=t11.T11_NOW,
    )
    session.add(sibling)
    session.commit()
    return report_id, int(sibling.id)


def _check_access(client: TestClient, report_id: int, private_id: int) -> dict[str, Any]:
    """Anonymous, viewer, editor, and admin access — through the UI and the API."""
    anonymous = client.get(UI, follow_redirects=False)
    anonymous_location = anonymous.headers.get("location", "")
    anonymous_redirect = anonymous.status_code == 303 and anonymous_location.startswith("/ui/login")

    viewer_login = _login(client, "viewer")
    viewer_list = client.get(UI)
    viewer_internal_page = client.get(f"{UI}/{report_id}")
    viewer_private_page = client.get(f"{UI}/{private_id}")
    viewer_write = client.post(f"{UI}/{report_id}/verify", data={"csrf_token": _csrf(client)})
    csrf_mismatch = client.post(
        f"{UI}/{report_id}/verify",
        data={"csrf_token": "not-the-issued-token"},
    )
    csrf_missing = client.post(f"{UI}/{report_id}/verify", data={})

    viewer_headers = _bearer(client, "viewer")
    editor_headers = _bearer(client, "editor")
    admin_headers = _bearer(client, "admin")
    viewer_ids = _api_list_ids(client, viewer_headers)
    editor_ids = _api_list_ids(client, editor_headers)
    admin_ids = _api_list_ids(client, admin_headers)

    viewer_private = client.get(f"{API}/{private_id}", headers=viewer_headers)
    admin_private = client.get(f"{API}/{private_id}", headers=admin_headers)
    missing = client.get(f"{API}/987654321", headers=admin_headers)

    return {
        "ui_anonymous_redirect": anonymous_redirect,
        "ui_anonymous_location": anonymous_location,
        "ui_viewer_login_status": viewer_login.status_code,
        "ui_viewer_session_cookie": bool(client.cookies.get(SESSION_COOKIE)),
        "ui_viewer_sees_internal": f"{UI}/{report_id}" in viewer_list.text,
        "ui_viewer_hides_private": f"{UI}/{private_id}" not in viewer_list.text,
        "ui_viewer_internal_detail_status": viewer_internal_page.status_code,
        "ui_viewer_private_detail_status": viewer_private_page.status_code,
        "ui_viewer_write_status": viewer_write.status_code,
        "ui_csrf_mismatch_status": csrf_mismatch.status_code,
        "ui_csrf_missing_status": csrf_missing.status_code,
        "api_viewer_sees_internal": report_id in viewer_ids,
        "api_viewer_hides_private": private_id not in viewer_ids,
        "api_editor_sees_private": private_id in editor_ids,
        "api_admin_sees_private": private_id in admin_ids,
        "api_viewer_private_detail_status": viewer_private.status_code,
        "api_admin_private_detail_status": admin_private.status_code,
        "api_missing_report_status": missing.status_code,
        "api_missing_report_code": missing.json().get("code", ""),
    }


def _edit_and_roll_back(
    client: TestClient,
    factory: sessionmaker[Session],
    report_id: int,
) -> dict[str, Any]:
    """Verify, edit the content, then restore the first version as a new one."""
    token = {"csrf_token": _csrf(client)}
    before = _snapshot(factory, report_id)

    verified = client.post(f"{UI}/{report_id}/verify", data=token)
    verdict = _verdict(factory, report_id, before["version"])

    edited = client.post(
        f"{UI}/{report_id}/edit",
        data={**token, "content_markdown": before["content"] + EDIT_NOTE},
    )
    after_edit = _snapshot(factory, report_id)

    rolled = client.post(
        f"{UI}/{report_id}/rollback",
        data={**token, "version": 1, "comment": "回退到第一版口径"},
    )
    after_rollback = _snapshot(factory, report_id)

    restored_from = 0
    for reason in reversed(after_rollback["change_reasons"]):
        match = re.match(r"rollback_to_v(\d+)", reason or "")
        if match:
            restored_from = int(match.group(1))
            break

    return {
        "ui_verify_flash": "验证结论：passed" in verified.text,
        "verification_verdict": verdict,
        "edit_flash": f"内容已更新为第 {after_edit['version']} 版" in edited.text,
        "edit_version": after_edit["version"],
        "edit_status": after_edit["status"],
        "edit_source_versions": after_edit["source_versions"],
        "rollback_flash": "已回滚到第 1 版" in rolled.text,
        "rollback_version": after_rollback["version"],
        "rollback_restored_from": restored_from,
        "rollback_status": after_rollback["status"],
        "rollback_source_versions": after_rollback["source_versions"],
        "rollback_content_restored": after_rollback["content"] == before["content"],
    }


def _review_and_publish(
    session: Session,
    client: TestClient,
    factory: sessionmaker[Session],
    report_id: int,
) -> dict[str, Any]:
    """Drive the `MANUAL` review loop, the approval, the release, and a post-publish rollback."""
    report = session.get(Report, report_id)
    if report is None:
        raise RuntimeError("report vanished before the review loop")
    report.publish_mode = "MANUAL"
    session.commit()

    token = {"csrf_token": _csrf(client)}

    queued = client.post(f"{UI}/{report_id}/publish", data=token)
    queued_state = _snapshot(factory, report_id)

    returned = client.post(
        f"{UI}/{report_id}/review",
        data={**token, "decision": "request_changes", "comment": "补充趋势对比"},
    )
    returned_state = _snapshot(factory, report_id)

    # A returned report is editable again, which puts it back in the draft queue.
    edited = client.post(
        f"{UI}/{report_id}/edit",
        data={**token, "content_markdown": returned_state["content"] + SECOND_EDIT_NOTE},
    )
    redrafted_state = _snapshot(factory, report_id)

    client.post(f"{UI}/{report_id}/publish", data=token)
    approved = client.post(
        f"{UI}/{report_id}/review",
        data={**token, "decision": "approve", "comment": "来源与数字已核对"},
    )
    approved_state = _snapshot(factory, report_id)

    released = client.post(f"{UI}/{report_id}/publish", data=token)
    published_state = _snapshot(factory, report_id)

    rolled = client.post(
        f"{UI}/{report_id}/rollback",
        data={**token, "version": 1, "comment": "发布后发现口径问题"},
    )
    final_state = _snapshot(factory, report_id)

    sweep = client.post(f"{UI}/publish-due", data=token)

    return {
        "queue_flash": "已转入人工审核" in queued.text,
        "review_request_status": queued_state["status"],
        "return_flash": "审核已记录：退回修改" in returned.text,
        "returned_status": returned_state["status"],
        "redraft_flash": f"内容已更新为第 {redrafted_state['version']} 版" in edited.text,
        "redraft_status": redrafted_state["status"],
        "redraft_version": redrafted_state["version"],
        "approve_flash": "审核已记录：通过" in approved.text,
        "approved_status": approved_state["status"],
        "release_flash": "报告已发布" in released.text,
        "published_status": published_state["status"],
        "final_rollback_flash": "已回滚到第 1 版" in rolled.text,
        "final_status": final_state["status"],
        "final_version": final_state["version"],
        "final_source_versions": final_state["source_versions"],
        "final_versions_recorded": final_state["versions"],
        "sweep_status": sweep.status_code,
        "sweep_rendered": "释放" in sweep.text,
    }


def run_checks(
    session: Session,
    factory: sessionmaker[Session],
    client: TestClient,
    ids: dict[str, int],
    actor_ids: dict[str, int],
) -> dict[str, Any]:
    """Drive the real application through the whole T12 acceptance path."""
    report_id, private_id = _seed_report(session, ids, actor_ids)
    access = _check_access(client, report_id, private_id)

    editor_login = _login(client, "editor")
    edit_and_rollback = _edit_and_roll_back(client, factory, report_id)
    review_and_publish = _review_and_publish(session, client, factory, report_id)

    final_state = _snapshot(factory, report_id)
    return {
        "report_id": report_id,
        "private_report_id": private_id,
        "report_title": final_state["title"],
        "ui_editor_login_status": editor_login.status_code,
        **access,
        **edit_and_rollback,
        **review_and_publish,
        "audit_actions": _audit_actions(factory, report_id),
        "verification_rows": _rows(factory, ReportVerification, report_id),
        "review_rows": _rows(factory, ReportReview, report_id),
    }


def _parse_args() -> argparse.Namespace:
    """Parse the optional cleanup mode used by the verification script."""
    parser = argparse.ArgumentParser(description="Run T12 report API and admin UI smoke checks")
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Only remove verification records and exit",
    )
    return parser.parse_args()


def main() -> None:
    """Run the T12 smoke checks on real MySQL and print the assertion payload."""
    args = _parse_args()
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    factory = create_session_factory(engine)

    actor_ids: dict[str, int] = {}
    payload: dict[str, Any] = {}
    try:
        with factory() as session:
            # `create_user` is keyed by username, so the previous run's actors must go first;
            # their reports go with them.
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
            with _client(settings) as client:
                payload = run_checks(session, factory, client, ids, actor_ids)
    finally:
        with factory() as session:
            _cleanup(session, actor_ids)
        engine.dispose()

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
