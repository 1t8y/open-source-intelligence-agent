"""Server-rendered admin UI: report list, report detail, and the review workflow.

The pages are rendered with Jinja2 and enhanced with HTMX, which is vendored under
`static/` so the interface works without any outbound network access. Every page reuses the
same service layer as the JSON API; the UI never touches SQL itself.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, FastAPI, Form, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.backfill.jobs import JOB_STATUSES, list_jobs, summarize_jobs
from open_source_intelligence.core.config import Settings
from open_source_intelligence.core.logging import redact_text
from open_source_intelligence.evaluation.dataset import list_datasets
from open_source_intelligence.evaluation.runner import list_runs as list_eval_runs
from open_source_intelligence.notifications.errors import PushError
from open_source_intelligence.notifications.service import (
    PUSHABLE_STATUSES,
    list_pushes,
    list_targets,
    push_report,
)
from open_source_intelligence.observability.metrics import (
    DEFAULT_WINDOW_DAYS,
    MAX_WINDOW_DAYS,
    MIN_WINDOW_DAYS,
)
from open_source_intelligence.observability.queries import (
    collect_metrics,
    get_agent_run_trace,
    list_agent_runs,
)
from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.agent import AGENT_RUN_STATUSES
from open_source_intelligence.persistence.models.audit import AUDIT_RESULTS
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import TOPIC_PUBLISH_MODES, Topic
from open_source_intelligence.persistence.models.notifications import (
    PUSH_STATUSES,
    PushTarget,
)
from open_source_intelligence.persistence.models.reporting import (
    REPORT_STATUSES,
    REPORT_VISIBILITIES,
    Report,
)
from open_source_intelligence.pipeline.queries import SORT_OPTIONS as REPOSITORY_SORT_OPTIONS
from open_source_intelligence.pipeline.queries import list_repositories
from open_source_intelligence.reports import queries
from open_source_intelligence.reports.errors import ReportGenerationError, ReportPublishError
from open_source_intelligence.reports.publishing import (
    publish_due_reports,
    publish_or_request_review,
    publish_report,
    record_review,
)
from open_source_intelligence.reports.revisions import (
    EDITABLE_STATUSES,
    ROLLBACKABLE_STATUSES,
    revise_report,
    rollback_report,
)
from open_source_intelligence.reports.sharing import (
    SHAREABLE_SENSITIVITY,
    SHAREABLE_STATUS,
    ShareError,
    ShareLinkNotFound,
    create_share_link,
    list_share_links,
    revoke_share_link,
)
from open_source_intelligence.reports.verification import verify_report
from open_source_intelligence.security.audit import format_ip_address, record_audit_event
from open_source_intelligence.security.authorization import (
    AUDIT_READ_ROLES,
    METRICS_ROLES,
    REPORT_READ_ROLES,
    REPORT_WRITE_ROLES,
    REPOSITORY_READ_ROLES,
    ROLE_ADMIN,
    TOPIC_READ_ROLES,
    TOPIC_WRITE_ROLES,
    can_read_report,
    can_read_topic,
    readable_reports_filter,
)
from open_source_intelligence.security.dependencies import (
    client_ip,
    get_db_session,
    request_id,
)
from open_source_intelligence.security.queries import list_audit_logs
from open_source_intelligence.security.service import authenticate_user, get_role_codes
from open_source_intelligence.security.tokens import (
    ACCESS_TOKEN_EXPIRES_SECONDS,
    create_access_token,
)
from open_source_intelligence.topics import (
    MAX_PAGE_SIZE as TOPIC_MAX_PAGE_SIZE,
)
from open_source_intelligence.topics import (
    TopicError,
    create_topic,
    delete_topic,
    list_topics,
    reference_counts,
    update_topic,
)

from .security import (
    CSRF_COOKIE,
    CSRF_FIELD,
    SESSION_COOKIE,
    WebForbidden,
    WebNotFound,
    WebRedirect,
    WebSession,
    cookie_secure,
    load_web_session,
    new_csrf_token,
    verify_csrf,
)

MODULE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = MODULE_DIR / "templates"
STATIC_DIR = MODULE_DIR / "static"

#: The windows the dashboard offers as buttons. The endpoints accept any 1-90 and refuse anything
#: else rather than silently clamping it; these three are the ones an operator actually asks for.
WINDOW_OPTIONS = (1, 7, 30)
#: How many recent runs the dashboard lists before pointing at the full ledger.
DASHBOARD_RUN_PAGE_SIZE = 10
#: How many rows the list pages inside the sidebar shell show per page. The page size is fixed
#: rather than a query parameter: these are browse pages, and a caller who wants a different page
#: size wants the JSON API.
LIST_PAGE_SIZE = 20
#: How many delivery records and ledger rows the "one screenful" pages show. `list_pushes`,
#: `list_jobs` and `list_eval_runs` take a limit rather than a page, so the page states the cap
#: instead of pretending to paginate.
RECENT_LIMIT = 50

#: Roles that may see an **operations-configuration** surface in the interface. Today that is the
#: push-target allowlist and the empty-state sentences that tell a reader to go write one.
#:
#: Managing those is deliberately not something the console does. A push target decides which
#: destinations a published report may leave through, and an account is created by the bootstrap
#: CLI; both are deployment-adjacent configuration, so the backend owns them and the interface shows
#: the state. Where the state is unavoidable — "you cannot push yet" needs to say why — the
#: explanation is rendered for an admin, who is the person who can act on it. `AUDIT_READ_ROLES` and
#: `TOPIC_WRITE_ROLES` are the same single role for the same reason: these are the controls that
#: change what the platform does, not what one report says.
OPS_CONFIG_ROLES: Final[tuple[str, ...]] = (ROLE_ADMIN,)

#: The sidebar, in the order it is shown: `(group title, ((key, label, path, roles), ...))`.
#: Declared next to the routes on purpose — the `roles` of an entry are the same roles its route
#: enforces, so a page cannot be listed for an identity that would then be refused when it opens.
NAV_GROUPS: tuple[tuple[str, tuple[tuple[str, str, str, tuple[str, ...]], ...]], ...] = (
    (
        "报告",
        (
            ("reports", "报告中心", "/ui/reports", REPORT_READ_ROLES),
            ("pushes", "推送记录", "/ui/pushes", REPORT_WRITE_ROLES),
        ),
    ),
    (
        "数据",
        (
            ("topics", "主题管理", "/ui/topics", TOPIC_READ_ROLES),
            ("repositories", "仓库目录", "/ui/repositories", REPOSITORY_READ_ROLES),
            ("runs", "采集与运行", "/ui/runs", METRICS_ROLES),
        ),
    ),
    (
        "质量",
        (("evaluations", "评测", "/ui/evaluations", REPORT_WRITE_ROLES),),
    ),
    (
        "运维",
        (
            ("dashboard", "运维看板", "/ui/dashboard", METRICS_ROLES),
            ("audit", "审计日志", "/ui/audit", AUDIT_READ_ROLES),
        ),
    ),
)

router = APIRouter(prefix="/ui", tags=["ui"], include_in_schema=False)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _redact(value: object) -> object:
    """Jinja filter applying the log redaction rule to one free-text value."""
    return None if value is None else redact_text(str(value))


def _redacted_json(value: object) -> str:
    """Jinja filter serializing a JSON column for display with credentials removed.

    The run trace is rendered by the UI as well as served by the JSON API, and both are egress
    paths. Reusing `redact_text` keeps one definition of what may leave the process instead of
    letting the page show what the endpoint deliberately hides.
    """
    return redact_text(json.dumps(value, ensure_ascii=False, default=str))


templates.env.filters["redact"] = _redact
templates.env.filters["redacted_json"] = _redacted_json


#: Display labels for the enum codes the platform stores. **Only the label is translated — the
#: value stays the contract code.** A select submits its `value`, so an option whose value became
#: 待审核 would send a state the server does not have; the same split applies to the code shown in
#: a badge versus the code sent in a form.
#:
#: Identifiers are deliberately absent and pass through untranslated: error codes
#: (`AGENT_EVIDENCE_INCOMPLETE`), audit action names (`topic.create`), evaluation target names
#: (`agent_full`) and role codes (`ADMIN`) are things an operator greps for in the contract, and a
#: translated copy would only be harder to match.
ENUM_LABELS: dict[str, str] = {
    # Report lifecycle.
    "draft": "草稿",
    "pending_review": "待审核",
    "changes_requested": "退回修改",
    "approved": "已批准",
    "scheduled": "待发布",
    "published": "已发布",
    # Run and job lifecycle. `pending`/`failed` are shared by several of these state machines.
    "pending": "待处理",
    "queued": "排队中",
    "running": "运行中",
    "succeeded": "成功",
    "success": "成功",
    "failed": "失败",
    "partial": "部分成功",
    "completed": "已完成",
    "cancelled": "已取消",
    "needs_review": "待人工确认",
    "denied": "被拒",
    "sent": "已送达",
    "passed": "通过",
    # Visibility, sensitivity, and the two publish modes (report and topic share the vocabulary).
    "PRIVATE": "私有",
    "TEAM": "团队",
    "INTERNAL": "内部",
    "PUBLIC": "公开",
    "CONFIDENTIAL": "机密",
    "AUTO": "自动",
    "AUTO_WITH_DELAY": "延迟自动",
    "MANUAL": "手动",
    # Delivery channels.
    "email": "邮件",
    "webhook": "Webhook",
}


def _label(value: object) -> str:
    """Translate one enum code for display, falling back to the code itself.

    The fallback matters: an unmapped value shows as `agent_full` rather than as nothing, so a new
    state added to a model appears on the page as its own code and is immediately visible as
    "not yet labelled" instead of as a blank cell.
    """
    text = "" if value is None else str(value)
    return ENUM_LABELS.get(text, text)


templates.env.filters["label"] = _label

DbSession = Annotated[Session, Depends(get_db_session)]
#: The alias matters: the templates post the token as `csrf_token`, and FastAPI binds a form
#: parameter by its declared name unless told otherwise. Paired with an `= ""` default at
#: every use site so a form that omits the token still reaches `verify_csrf` and gets the
#: rendered 403 page, instead of a JSON validation error.
CsrfField = Annotated[str, Form(alias=CSRF_FIELD)]


def get_web_session(request: Request, session: DbSession) -> WebSession:
    """FastAPI dependency wrapper around `load_web_session`."""
    return load_web_session(request, session)


WebSessionDep = Annotated[WebSession, Depends(get_web_session)]


def add_web_surface(app: FastAPI) -> None:
    """Mount the static assets and register the UI redirect/forbidden responses."""
    app.mount("/ui/static", StaticFiles(directory=str(STATIC_DIR)), name="ui-static")

    @app.exception_handler(WebRedirect)
    async def redirect_handler(_: Request, exc: WebRedirect) -> Response:
        return RedirectResponse(exc.location, status_code=exc.status_code)

    @app.exception_handler(WebForbidden)
    async def forbidden_handler(request: Request, exc: WebForbidden) -> Response:
        return templates.TemplateResponse(
            request,
            "forbidden.html",
            {"message": exc.message, "current_user": None, "csrf_token": ""},
            status_code=status.HTTP_403_FORBIDDEN,
        )

    @app.exception_handler(WebNotFound)
    async def not_found_handler(request: Request, exc: WebNotFound) -> Response:
        return templates.TemplateResponse(
            request,
            "not_found.html",
            {"message": exc.message, "current_user": None, "csrf_token": ""},
            status_code=status.HTTP_404_NOT_FOUND,
        )


def _settings(request: Request) -> Settings:
    """Return the application settings bound to the running app."""
    settings = request.app.state.settings
    assert isinstance(settings, Settings)
    return settings


def _identity(user: User) -> dict[str, Any]:
    """Render-friendly identity for the signed-in user."""
    return {"username": user.username, "roles": sorted(get_role_codes(user))}


def _roles_of(context: dict[str, Any]) -> frozenset[str]:
    """Return the role codes carried by the identity a page already holds.

    One reader for `context["current_user"]`, because three callers ask the same question of it:
    which sidebar entries to draw, whether to draw the dashboard entry, and which operations
    surfaces this identity may see. A rule that read the dict for itself would be a fourth opinion
    on what "signed in with a role" means, and the four would drift.
    """
    identity = context.get("current_user")
    if not isinstance(identity, dict):
        return frozenset()
    declared = identity.get("roles")
    if not isinstance(declared, (list, tuple, set)):
        return frozenset()
    return frozenset(str(role) for role in declared)


def _has_role(context: dict[str, Any], allowed: tuple[str, ...]) -> bool:
    """Return whether the page's identity intersects `allowed`.

    Written as an intersection rather than `in`: a rule gated on one role today would silently keep
    working if the tuple grows, while `role in allowed` would not.
    """
    return not _roles_of(context).isdisjoint(allowed)


def _sees_ops_config(web: WebSession) -> bool:
    """Return whether this session may see an operations-configuration surface.

    The entity-level twin of the `can_manage_ops` the renderer derives from an identity. A route
    sometimes has to decide *before* a template exists — whether to load the push-target allowlist
    at all — and it cannot ask the renderer. Both read `OPS_CONFIG_ROLES`, so the data a page
    carries and the card that shows it cannot disagree.
    """
    return not get_role_codes(web.user).isdisjoint(OPS_CONFIG_ROLES)


def _navigation(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the sidebar for the identity a page already carries.

    Derived from `context["current_user"]` for the same reason `can_manage_ops` is: every template
    renders the nav, and none of them should have to compute who may see what. An identity lacking a
    group's roles drops the whole entry, and a group left with no entries drops the heading too — an
    empty group title is a promise of a page that is not there.
    """
    roles = _roles_of(context)
    navigation: list[dict[str, Any]] = []
    for title, entries in NAV_GROUPS:
        items = [
            {"key": key, "label": label, "href": path}
            for key, label, path, allowed in entries
            if not roles.isdisjoint(allowed)
        ]
        if items:
            # The list is keyed `entries`, not `items`: Jinja resolves `group.items` on a dict to
            # the built-in bound method before it ever looks at the key, so `{% for x in
            # group.items %}` raises "not iterable". Renaming is cheaper than a rule nobody
            # remembers at the next template.
            navigation.append({"title": title, "entries": items})
    return navigation


def render(
    request: Request,
    name: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
    csrf: str | None = None,
) -> HTMLResponse:
    """Render a template and hand the browser a CSRF cookie when it lacks one."""
    payload: dict[str, Any] = {
        "csrf_token": csrf or request.cookies.get(CSRF_COOKIE, ""),
        # Hand the state-machine-derived sets to the template instead of retyping them there, so
        # the buttons a page offers and the states the services accept cannot drift apart.
        "editable_statuses": sorted(EDITABLE_STATUSES),
        "rollbackable_statuses": sorted(ROLLBACKABLE_STATUSES),
        # The sidebar and its current entry: one decision point per rule, computed here so no
        # template has to know which role sees which page.
        "navigation": _navigation(context),
        # Which operations surfaces this identity may see. Same reasoning as the sidebar, and the
        # same shape, so a page that renders an operations detail cannot be wider than the route
        # that serves it.
        "can_manage_ops": _has_role(context, OPS_CONFIG_ROLES),
        "active": context.get("active"),
    }
    payload.update(context)
    response = templates.TemplateResponse(
        request,
        name,
        payload,
        status_code=status_code,
    )
    if csrf and request.cookies.get(CSRF_COOKIE) != csrf:
        response.set_cookie(
            CSRF_COOKIE,
            csrf,
            httponly=True,
            samesite="lax",
            secure=cookie_secure(_settings(request)),
            path="/",
        )
    return response


@contextmanager
def _audited_action(
    request: Request,
    session: Session,
    web: WebSession,
    *,
    resource_type: str,
    resource_id: str | None = None,
) -> Iterator[None]:
    """Record a UI refusal before it is rendered.

    The JSON API writes an audit row for every denial; the UI rendered the same refusal and
    wrote nothing, so the trail only ever held the API's half of the story. §3.1.1 of
    `docs/security-and-privacy.md` asks for the audit on the *write*, not on the transport, so
    the UI has to record it too.

    It is recorded here rather than in the `WebForbidden` handler because the handler runs
    outside the request's dependency graph and has no database session to write through.
    """
    try:
        yield
    except WebForbidden as exc:
        record_audit_event(
            session,
            action=exc.action,
            resource_type=resource_type,
            resource_id=resource_id,
            result="denied",
            actor_id=web.user.id,
            request_id=request_id(request),
            ip_address=client_ip(request),
        )
        session.commit()
        raise


def _require_write_role(session: WebSession) -> None:
    """Reject a mutating UI action from a read-only role."""
    if get_role_codes(session.user).isdisjoint(REPORT_WRITE_ROLES):
        raise WebForbidden("只读角色不能执行该操作")


def _require_roles(
    request: Request,
    session: Session,
    web: WebSession,
    allowed: tuple[str, ...],
    *,
    resource_type: str,
    message: str,
) -> None:
    """Reject an identity the page's roles do not cover, recording the refusal.

    One helper for every page that is not open to everyone: the audit action, the resource type
    and the refusal path are the same on each of them, and a page that spelled its own version
    would be a page whose refusals are filed under a different name than the rest.
    """
    with _audited_action(request, session, web, resource_type=resource_type):
        if get_role_codes(web.user).isdisjoint(allowed):
            raise WebForbidden(message)


def _require_metrics_role(request: Request, session: Session, web: WebSession) -> None:
    """Reject a viewer from the operations dashboard.

    The dashboard exposes cross-topic cost, failure reasons and evaluation verdicts rather than
    the contents of one report, so it follows `METRICS_ROLES` instead of the report read roles.
    """
    _require_roles(
        request,
        session,
        web,
        METRICS_ROLES,
        resource_type="metrics",
        message="该页面仅对管理员和编辑者开放",
    )


def _authorize_action(
    request: Request,
    session: Session,
    web: WebSession,
    report_id: int,
    csrf: str | None,
) -> None:
    """Guard a mutating UI action: CSRF, then write role, then read access.

    The read check runs *before* the service call so an action can never take effect on a
    report the caller may not see, and any of the three refusals is audited as it is raised.
    """
    with _audited_action(request, session, web, resource_type="report", resource_id=str(report_id)):
        verify_csrf(request, csrf)
        _require_write_role(web)
        _load_visible_report(session, web, report_id)


def _load_visible_report(session: Session, web: WebSession, report_id: int) -> Report:
    """Load a report the current user may read, or render the matching error page.

    The refusal is filed as `report.read`, the same action the JSON API records, so "who tried
    to reach which report" is one query over one action name rather than two.
    """
    report = session.get(Report, report_id)
    if report is None:
        raise WebNotFound(f"报告 {report_id} 不存在")
    if not can_read_report(web.user, report, get_role_codes(web.user)):
        raise WebForbidden("当前用户无权查看该报告", action="report.read")
    return report


def _topic_name(session: Session, topic_id: int) -> str | None:
    """Return one topic name."""
    return session.scalar(select(Topic.name).where(Topic.id == topic_id))


def _topic_names(session: Session, topic_ids: set[int]) -> dict[int, str]:
    """Map topic ids to names for the list pages.

    A presentation-only name lookup, not a read model: the list queries return rows that carry a
    `topic_id`, and the page has to print something a human recognises. Same shape as the lookup
    `reports_page` does inline — pulled out here so the repository page and the topic page use the
    one copy instead of each inventing their own.
    """
    if not topic_ids:
        return {}
    rows = session.execute(select(Topic.id, Topic.name).where(Topic.id.in_(topic_ids))).all()
    return {int(row[0]): str(row[1]) for row in rows}


def _report_titles(session: Session, report_ids: set[int]) -> dict[int, str]:
    """Map report ids to titles for the list pages. See `_topic_names`."""
    if not report_ids:
        return {}
    rows = session.execute(select(Report.id, Report.title).where(Report.id.in_(report_ids))).all()
    return {int(row[0]): str(row[1]) for row in rows}


def _push_context(session: Session, report_id: int) -> dict[str, Any]:
    """Build the data the push card needs: the allowlist and this report's delivery trail."""
    return {
        "push_targets": list_targets(session),
        "pushes": list_pushes(session, report_id=report_id, limit=20),
        "push_statuses": sorted(PUSHABLE_STATUSES),
    }


def _research_trace_context(session: Session, report: Report) -> dict[str, Any]:
    """Build the data the collapsed "research trace" card needs.

    The trace is the Agent's step-by-step trajectory (what it planned, which tools it called,
    what each returned). It is deliberately *collapsed* on the report page: the report's value is
    the finished narrative, not the machinery, so the trace is available to inspect on demand
    without pushing its latency and token columns in front of the reader.
    """
    trace = (
        get_agent_run_trace(session, run_id=report.agent_run_id) if report.agent_run_id else None
    )
    return {"trace": trace}


def _runtime_mode(settings: Settings) -> dict[str, Any]:
    """Describe how the deployment is wired, so a page can say "offline demo" vs "real".

    The three facts that decide whether the platform is running for real — the model provider,
    the delivery transport, and whether research auto-dispatches — are surfaced together so a
    reader cannot mistake an offline stub product for a real one. `provider` is "offline" only
    for the deterministic stub; any remote provider counts as real.
    """
    provider_real = settings.agent_provider != "offline"
    delivery_real = settings.push_delivery == "live"
    return {
        "provider": settings.agent_provider,
        "provider_real": provider_real,
        "delivery": settings.push_delivery,
        "delivery_real": delivery_real,
        "auto_research": settings.agent_auto_research_enabled,
        "fully_real": provider_real and delivery_real,
    }


def _delivery_mode(settings: Settings) -> dict[str, Any]:
    """Describe how delivery is configured so the ledger page can say it out loud.

    The answer is the same three facts the service acts on — `push_delivery`, whether pushing is
    on, and whether the webhook channel carries a signing secret — plus the two test-only escape
    hatches that a local drill flips. A reader should not have to infer "these are all offline
    receipts" from a table full of `offline:` details.
    """
    return {
        "delivery": settings.push_delivery,
        "enabled": settings.push_enabled,
        "webhook_signed": settings.push_webhook_signing_secret is not None,
        "allow_private_targets": settings.push_webhook_allow_private_targets,
        "custom_ca": settings.push_webhook_ca_bundle is not None,
    }


def _share_context(session: Session, report: Report) -> dict[str, Any]:
    """Build the data the share card needs: the links and whether a new one may be minted.

    The eligibility answer is computed from the same two constants the service refuses on
    (`SHAREABLE_SENSITIVITY`, `SHAREABLE_STATUS`), so the card cannot offer a button that the
    service will reject - the same rule the navigation and topic pages follow.
    """
    shareable = report.sensitivity == SHAREABLE_SENSITIVITY and report.status == SHAREABLE_STATUS
    if report.sensitivity != SHAREABLE_SENSITIVITY:
        blocker = f"敏感性为 {report.sensitivity}，只有 {SHAREABLE_SENSITIVITY} 的报告可以对外分享"
    elif report.status != SHAREABLE_STATUS:
        blocker = f"报告状态为 {report.status}，只有 {SHAREABLE_STATUS} 的报告可以对外分享"
    else:
        blocker = None
    return {
        "share_links": list_share_links(session, report_id=report.id),
        "shareable": shareable,
        "share_blocker": blocker,
    }


# --------------------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next_url: Annotated[str, Query(alias="next")] = "/ui/reports",
) -> HTMLResponse:
    """Render the login form."""
    csrf = new_csrf_token()
    return render(
        request,
        "login.html",
        {"next": next_url, "error": None, "current_user": None},
        csrf=csrf,
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    session: DbSession,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next_url: Annotated[str, Form(alias="next")] = "/ui/reports",
    csrf: CsrfField = "",
) -> Response:
    """Authenticate the form and store the access token in an HttpOnly cookie."""
    verify_csrf(request, csrf)
    user = authenticate_user(session, username, password)
    if user is None:
        record_audit_event(
            session,
            action="auth.login",
            resource_type="auth",
            result="failed",
            request_id=request_id(request),
            ip_address=client_ip(request),
        )
        session.commit()
        return render(
            request,
            "login.html",
            {"next": next_url, "error": "用户名或密码不正确", "current_user": None},
            status_code=status.HTTP_401_UNAUTHORIZED,
            csrf=new_csrf_token(),
        )
    token = create_access_token(user, _settings(request))
    record_audit_event(
        session,
        action="auth.login",
        resource_type="auth",
        result="success",
        actor_id=user.id,
        request_id=request_id(request),
        ip_address=client_ip(request),
    )
    session.commit()
    destination = next_url if next_url.startswith("/ui/") else "/ui/reports"
    response = RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=cookie_secure(_settings(request)),
        max_age=ACCESS_TOKEN_EXPIRES_SECONDS,
        path="/",
    )
    return response


@router.post("/logout")
def logout(request: Request, csrf: CsrfField = "") -> Response:
    """Clear the session cookie."""
    verify_csrf(request, csrf)
    response = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# --------------------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------------------


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    now: Annotated[datetime, Depends(utc_now)],
    window_days: Annotated[
        int, Query(ge=MIN_WINDOW_DAYS, le=MAX_WINDOW_DAYS)
    ] = DEFAULT_WINDOW_DAYS,
) -> HTMLResponse:
    """Render the operations dashboard: one window of metrics plus the newest runs.

    The role check runs before anything is read, so a viewer's request never touches the ledger
    and the page they get back says why rather than showing an empty dashboard.
    """
    _require_metrics_role(request, session, web)
    overview = collect_metrics(
        session,
        now=now,
        window_days=window_days,
        settings=_settings(request),
    )
    recent_runs = list_agent_runs(session, page=1, page_size=DASHBOARD_RUN_PAGE_SIZE)
    return render(
        request,
        "dashboard.html",
        {
            "current_user": _identity(web.user),
            "overview": overview,
            "recent_runs": recent_runs,
            "window_days": overview.window.days,
            "window_options": WINDOW_OPTIONS,
            "runtime_mode": _runtime_mode(_settings(request)),
            "active": "dashboard",
        },
    )


@router.get("/dashboard/runs/{run_id}", response_class=HTMLResponse)
def dashboard_run_page(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    run_id: int,
) -> HTMLResponse:
    """Render one run's trajectory: its ordered steps and the tool calls under each of them.

    This is the page the "从失败报告追到具体工具调用" acceptance is walked on: an operator sees
    the run in the dashboard, opens it, and reaches the exact tool call that failed with its
    error code. The role check runs first, as on the dashboard itself.
    """
    _require_metrics_role(request, session, web)
    trace = get_agent_run_trace(session, run_id=run_id)
    if trace is None:
        raise WebNotFound(f"运行 {run_id} 不存在")
    return render(
        request,
        "dashboard_run.html",
        {
            "current_user": _identity(web.user),
            "trace": trace,
            "unfinished": trace.run.started_at is None or trace.run.finished_at is None,
            "active": "dashboard",
        },
    )


# --------------------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------------------


@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    visibility: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> HTMLResponse:
    """Render the paginated, access-filtered report list."""
    page_result = queries.list_reports(
        session,
        access_filter=readable_reports_filter(web.user, get_role_codes(web.user)),
        status=status_filter,
        visibility=visibility,
        page=page,
        page_size=page_size,
    )
    topic_names = _topic_names(session, {item.topic_id for item in page_result.items})
    return render(
        request,
        "reports.html",
        {
            "current_user": _identity(web.user),
            "page": page_result,
            "topic_names": topic_names,
            "status_filter": status_filter or "",
            "visibility_filter": visibility or "",
            # Offered from the model's own tuples rather than retyped in the template: a filter
            # that lists a state the state machine does not have returns an empty page and looks
            # like a bug in the data.
            "status_options": REPORT_STATUSES,
            "visibility_options": REPORT_VISIBILITIES,
            # The release driver on this page is a write: the route behind it enforces
            # `REPORT_WRITE_ROLES`, so the button is rendered under the same rule. A read-only
            # identity used to be offered the button and refused by the route, which is the same
            # defect as a sidebar entry that opens to a 403.
            "can_write": not get_role_codes(web.user).isdisjoint(REPORT_WRITE_ROLES),
            "active": "reports",
        },
        csrf=request.cookies.get(CSRF_COOKIE) or new_csrf_token(),
    )


def _panel(
    request: Request,
    session: Session,
    web: WebSession,
    report_id: int,
    *,
    flash: str | None = None,
    error: str | None = None,
    share_token: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render the report panel that every action endpoint swaps back in."""
    report = _load_visible_report(session, web, report_id)
    detail = queries.get_report_detail(session, report_id=report.id)
    assert detail is not None
    return render(
        request,
        "partials/report_panel.html",
        {
            "current_user": _identity(web.user),
            "report": report,
            "detail": detail,
            "topic_name": _topic_name(session, report.topic_id),
            "flash": flash,
            "error": error,
            "can_write": not get_role_codes(web.user).isdisjoint(REPORT_WRITE_ROLES),
            # 明文令牌只在这一次响应里出现：_panel 处理的是 POST 的返回值，刷新页面会重发请求而不是
            # 重放这个字符串，因此"只显示一次"由 HTTP 语义保证，不靠前端清空变量。
            "share_token": share_token,
            **_push_context(session, report.id),
            **_share_context(session, report),
            **_research_trace_context(session, report),
        },
        status_code=status_code,
    )


@router.get("/reports/{report_id}", response_class=HTMLResponse)
def report_page(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    report_id: int,
) -> HTMLResponse:
    """Render one report with its citations, verdict, reviews, and actions."""
    report = _load_visible_report(session, web, report_id)
    detail = queries.get_report_detail(session, report_id=report.id)
    assert detail is not None
    return render(
        request,
        "report_detail.html",
        {
            "current_user": _identity(web.user),
            "report": report,
            "detail": detail,
            "topic_name": _topic_name(session, report.topic_id),
            "flash": None,
            "error": None,
            "can_write": not get_role_codes(web.user).isdisjoint(REPORT_WRITE_ROLES),
            "share_token": None,
            **_push_context(session, report.id),
            **_share_context(session, report),
            **_research_trace_context(session, report),
            "active": "reports",
        },
        csrf=request.cookies.get(CSRF_COOKIE) or new_csrf_token(),
    )


@router.post("/reports/{report_id}/verify", response_class=HTMLResponse)
def verify_action(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    report_id: int,
    csrf: CsrfField = "",
) -> HTMLResponse:
    """Run the Verifier and swap the refreshed panel back in."""
    _authorize_action(request, session, web, report_id, csrf)
    outcome = verify_report(session, report_id=report_id)
    reason = "、".join(outcome.reason_codes) or "无"
    return _panel(
        request,
        session,
        web,
        report_id,
        flash=f"验证结论：{outcome.verdict}（原因码：{reason}）",
    )


@router.post("/reports/{report_id}/review", response_class=HTMLResponse)
def review_action(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    report_id: int,
    decision: Annotated[str, Form()],
    comment: Annotated[str, Form()] = "",
    csrf: CsrfField = "",
) -> HTMLResponse:
    """Record one human review decision."""
    _authorize_action(request, session, web, report_id, csrf)
    try:
        outcome = record_review(
            session,
            report_id=report_id,
            decision=decision,
            reviewer_id=web.user.id,
            comment=comment or None,
        )
    except (ReportGenerationError, ReportPublishError) as exc:
        return _panel(request, session, web, report_id, error=str(exc))
    labels = {"approve": "通过", "reject": "拒绝", "request_changes": "退回修改"}
    return _panel(
        request,
        session,
        web,
        report_id,
        flash=f"审核已记录：{labels.get(outcome.decision, outcome.decision)}",
    )


@router.post("/reports/{report_id}/publish", response_class=HTMLResponse)
def publish_action(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    report_id: int,
    csrf: CsrfField = "",
) -> HTMLResponse:
    """Apply the publish policy and release the report when it is already due.

    A report still inside its withdrawal window stays queued here, exactly like the JSON API:
    the release is only attempted once the window has elapsed, so an `INV-5` block surfaces as
    a rendered 409 instead of a bare "queued".
    """
    _authorize_action(request, session, web, report_id, csrf)
    try:
        outcome = publish_or_request_review(session, report_id=report_id)
        released = False
        if (
            outcome.status == "scheduled"
            and outcome.publish_at is not None
            and outcome.publish_at <= utc_now()
        ):
            try:
                released = publish_report(session, report_id=report_id).status == "published"
            except ReportPublishError as exc:
                return _panel(
                    request,
                    session,
                    web,
                    report_id,
                    error=f"{exc.code}：{exc}",
                    status_code=status.HTTP_409_CONFLICT,
                )
    except (ReportGenerationError, ReportPublishError) as exc:
        return _panel(request, session, web, report_id, error=str(exc))
    if released:
        return _panel(request, session, web, report_id, flash="报告已发布")
    if outcome.requires_review:
        reasons = "、".join(outcome.reason_codes) or "MANUAL"
        return _panel(
            request,
            session,
            web,
            report_id,
            flash=f"已转入人工审核（原因码：{reasons}）",
        )
    window = outcome.publish_at.isoformat() if outcome.publish_at else "—"
    return _panel(
        request,
        session,
        web,
        report_id,
        flash=f"已进入发布队列，撤回窗口到期时间：{window}",
    )


@router.post("/reports/publish-due", response_class=HTMLResponse)
def publish_due_action(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    csrf: CsrfField = "",
) -> HTMLResponse:
    """Run the release driver once, mirroring the scheduled sweep."""
    with _audited_action(request, session, web, resource_type="report"):
        verify_csrf(request, csrf)
        _require_write_role(web)
    released = publish_due_reports(session)
    blocked = [entry for entry in released if entry["status"] == "failed"]
    message = f"释放 {len(released) - len(blocked)} 条，被拦截 {len(blocked)} 条"
    return render(
        request,
        "partials/sweep_result.html",
        {"current_user": _identity(web.user), "message": message, "entries": released},
    )


@router.post("/reports/{report_id}/push", response_class=HTMLResponse)
def push_action(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    report_id: int,
    target_id: Annotated[int, Form()],
    csrf: CsrfField = "",
) -> HTMLResponse:
    """Deliver the report to one allowlisted target and swap the refreshed panel back in.

    The channel is read from the target row rather than from the form, so the browser cannot
    ask for a channel the target does not use. Re-submitting the same target is the retry
    path: the service replays a delivered push and advances the attempt count of a failed one.
    """
    _authorize_action(request, session, web, report_id, csrf)
    target = session.get(PushTarget, target_id)
    channel = target.channel if target is not None else ""
    try:
        outcome = push_report(
            session,
            report_id=report_id,
            channel=channel,
            target_id=target_id,
            actor_id=web.user.id,
        )
    except PushError as exc:
        return _panel(request, session, web, report_id, error=f"{exc.code}：{exc}")
    if outcome.delivered:
        replay = "（该版本与目标此前已推送，未重复发送）" if outcome.replayed else ""
        return _panel(
            request,
            session,
            web,
            report_id,
            flash=f"已推送到 {outcome.destination}（第 {outcome.attempt} 次尝试）{replay}",
        )
    return _panel(
        request,
        session,
        web,
        report_id,
        error=(f"推送失败：{outcome.error_code}（第 {outcome.attempt} 次尝试，可再次推送重试）"),
    )


@router.post("/reports/{report_id}/rollback", response_class=HTMLResponse)
def rollback_action(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    report_id: int,
    version: Annotated[int, Form()],
    comment: Annotated[str, Form()] = "",
    csrf: CsrfField = "",
) -> HTMLResponse:
    """Restore a historical version as a new draft version."""
    _authorize_action(request, session, web, report_id, csrf)
    try:
        outcome = rollback_report(
            session,
            report_id=report_id,
            version=version,
            actor_id=web.user.id,
            comment=comment or None,
        )
    except (ReportGenerationError, ReportPublishError) as exc:
        return _panel(request, session, web, report_id, error=str(exc))
    return _panel(
        request,
        session,
        web,
        report_id,
        flash=f"已回滚到第 {outcome.restored_from} 版，新版本为第 {outcome.version} 版并回到草稿",
    )


@router.post("/reports/{report_id}/edit", response_class=HTMLResponse)
def edit_action(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    report_id: int,
    title: Annotated[str, Form()] = "",
    content_markdown: Annotated[str, Form()] = "",
    visibility: Annotated[str, Form()] = "",
    sensitivity: Annotated[str, Form()] = "",
    csrf: CsrfField = "",
) -> HTMLResponse:
    """Apply a manual edit from the detail page."""
    _authorize_action(request, session, web, report_id, csrf)
    try:
        outcome = revise_report(
            session,
            report_id=report_id,
            actor_id=web.user.id,
            title=title or None,
            content_markdown=content_markdown or None,
            visibility=visibility or None,
            sensitivity=sensitivity or None,
        )
    except (ReportGenerationError, ReportPublishError) as exc:
        return _panel(request, session, web, report_id, error=str(exc))
    if not outcome.content_changed:
        return _panel(
            request,
            session,
            web,
            report_id,
            flash=f"元数据已更新：{'、'.join(outcome.changed_fields) or '无变化'}",
        )
    return _panel(
        request,
        session,
        web,
        report_id,
        flash=f"内容已更新为第 {outcome.version} 版，报告回到草稿待重新验证",
    )


# --------------------------------------------------------------------------------------
# Push deliveries
# --------------------------------------------------------------------------------------


@router.post("/reports/{report_id}/share", response_class=HTMLResponse)
def create_share_action(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    report_id: int,
    csrf: CsrfField = "",
) -> HTMLResponse:
    """Mint a no-login link and render it once.

    The plaintext token is passed to this one render and never stored. A refresh re-posts the
    form (the browser asks first) instead of replaying the string, so "shown once" follows from
    HTTP semantics rather than from front-end bookkeeping.
    """
    _authorize_action(request, session, web, report_id, csrf)
    try:
        created = create_share_link(session, report_id=report_id, actor_id=web.user.id)
    except ShareError as exc:
        return _panel(
            request,
            session,
            web,
            report_id,
            error=f"{exc.code}：{exc}",
            status_code=status.HTTP_409_CONFLICT,
        )
    return _panel(
        request,
        session,
        web,
        report_id,
        share_token=created.token,
        flash="分享链接已生成。它只在这里显示一次，库内只存摘要 —— 关掉页面后无法找回。",
    )


@router.post("/reports/{report_id}/share/{share_id}/revoke", response_class=HTMLResponse)
def revoke_share_action(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    report_id: int,
    share_id: int,
    csrf: CsrfField = "",
) -> HTMLResponse:
    """Stop a link from resolving, idempotently."""
    _authorize_action(request, session, web, report_id, csrf)
    try:
        revoked = revoke_share_link(
            session,
            report_id=report_id,
            share_id=share_id,
            actor_id=web.user.id,
        )
    except ShareLinkNotFound as exc:
        return _panel(
            request,
            session,
            web,
            report_id,
            error=f"{exc.code}：{exc}",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    flash = "链接已撤回" if revoked else "该链接此前已撤回，无需重复操作"
    return _panel(request, session, web, report_id, flash=flash)


@router.get("/pushes", response_class=HTMLResponse)
def pushes_page(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> HTMLResponse:
    """Render the delivery ledger: which report version went to which destination.

    `list_pushes` caps rather than paginates, so the page prints its cap instead of offering
    paging controls the query could not honour.
    """
    _require_roles(
        request,
        session,
        web,
        REPORT_WRITE_ROLES,
        resource_type="push",
        message="推送记录仅对管理员和编辑者开放",
    )
    pushes = list_pushes(session, status=status_filter, limit=RECENT_LIMIT)
    # The allowlist is loaded only when its card will be drawn. The rendering rule and this one read
    # the same constant, so a viewer who is not shown the card does not have its contents in the
    # payload either — a display rule that leaves the data in place is a leak waiting for the next
    # debug block.
    targets = list_targets(session, enabled_only=False) if _sees_ops_config(web) else []
    return render(
        request,
        "pushes.html",
        {
            "current_user": _identity(web.user),
            "pushes": pushes,
            "targets": targets,
            "report_titles": _report_titles(session, {push.report_id for push in pushes}),
            "status_filter": status_filter or "",
            "status_options": PUSH_STATUSES,
            "limit": RECENT_LIMIT,
            "active": "pushes",
            "delivery": _delivery_mode(_settings(request)),
        },
    )


# --------------------------------------------------------------------------------------
# Repository catalog
# --------------------------------------------------------------------------------------


@router.get("/repositories", response_class=HTMLResponse)
def repositories_page(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    q: Annotated[str | None, Query()] = None,
    language: Annotated[str | None, Query()] = None,
    sort: Annotated[str | None, Query()] = None,
    topic_id: Annotated[int | None, Query(ge=1)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    """Render the repository catalog: what the collectors found, and under which topic."""
    with _audited_action(
        request,
        session,
        web,
        resource_type="topic",
        resource_id=str(topic_id) if topic_id is not None else None,
    ):
        if get_role_codes(web.user).isdisjoint(REPOSITORY_READ_ROLES):
            raise WebForbidden("当前角色不能查看仓库目录")
        # `list_repositories` documents that the caller settles topic visibility *before* calling
        # it: the catalog itself has no per-row access predicate, so narrowing to a topic the
        # caller may not read would otherwise be a way to read it one row at a time.
        if topic_id is not None:
            topic = session.get(Topic, topic_id)
            if topic is None:
                raise WebNotFound(f"主题 {topic_id} 不存在")
            if not can_read_topic(web.user, topic, get_role_codes(web.user)):
                raise WebForbidden("当前用户无权查看该主题的候选列表", action="topic.read")
    page_result = list_repositories(
        session,
        topic_id=topic_id,
        language=language or None,
        query=q or None,
        sort=sort or REPOSITORY_SORT_OPTIONS[0],
        page=page,
        page_size=LIST_PAGE_SIZE,
    )
    return render(
        request,
        "repositories.html",
        {
            "current_user": _identity(web.user),
            "page": page_result,
            # The filter offers the topics this identity can read, capped at the documented page
            # size: beyond that the list is not a filter any more, it is a second catalog.
            "topics": list_topics(session, user=web.user, page_size=TOPIC_MAX_PAGE_SIZE).items,
            "query": q or "",
            "language_filter": language or "",
            "sort_filter": sort or REPOSITORY_SORT_OPTIONS[0],
            "sort_options": REPOSITORY_SORT_OPTIONS,
            "topic_filter": topic_id,
            "active": "repositories",
        },
    )


# --------------------------------------------------------------------------------------
# Collection and research runs
# --------------------------------------------------------------------------------------


@router.get("/runs", response_class=HTMLResponse)
def runs_page(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    """Render the run ledger: Agent runs over their candidate pool, and the backfill walks.

    The two live on one page because they are the two halves of 采集与运行: a backfill job is the
    history walk that fills a topic's candidate pool, and an Agent run is the research pass over it.
    Both are `METRICS_ROLES` — they expose cross-topic cost and failure reasons, not one report.
    """
    _require_roles(
        request,
        session,
        web,
        METRICS_ROLES,
        resource_type="metrics",
        message="采集与运行仅对管理员和编辑者开放",
    )
    agent_runs = list_agent_runs(session, status=status_filter, page=page, page_size=LIST_PAGE_SIZE)
    return render(
        request,
        "runs.html",
        {
            "current_user": _identity(web.user),
            "page": agent_runs,
            "topic_names": _topic_names(session, {run.topic_id for run in agent_runs.items}),
            "jobs": summarize_jobs(list_jobs(session, limit=RECENT_LIMIT)),
            "status_filter": status_filter or "",
            "status_options": AGENT_RUN_STATUSES,
            "job_statuses": JOB_STATUSES,
            "limit": RECENT_LIMIT,
            "active": "runs",
        },
    )


# --------------------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------------------


@router.get("/evaluations", response_class=HTMLResponse)
def evaluations_page(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    dataset_id: Annotated[int | None, Query(ge=1)] = None,
) -> HTMLResponse:
    """Render the evaluation surface: the imported datasets and the runs over them.

    Read-only on purpose. Running an evaluation spends model budget, and the same reasoning that
    keeps `POST /agent/runs` and `POST /topics/{id}/run` off the API keeps the run trigger on the
    operator CLI (`python -m open_source_intelligence.evaluation.cli`).
    """
    _require_roles(
        request,
        session,
        web,
        REPORT_WRITE_ROLES,
        resource_type="evaluation",
        message="评测页仅对管理员和编辑者开放",
    )
    datasets = list_datasets(session, limit=RECENT_LIMIT)
    return render(
        request,
        "evaluations.html",
        {
            "current_user": _identity(web.user),
            "datasets": datasets,
            "runs": list_eval_runs(session, dataset_id=dataset_id, limit=RECENT_LIMIT),
            "dataset_names": {
                int(dataset.id): f"{dataset.name}@{dataset.version}" for dataset in datasets
            },
            "dataset_filter": dataset_id,
            "limit": RECENT_LIMIT,
            "active": "evaluations",
        },
    )


# --------------------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------------------


@router.get("/audit", response_class=HTMLResponse)
def audit_page(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    actor_id: Annotated[int | None, Query(ge=1)] = None,
    action: Annotated[str | None, Query()] = None,
    resource_type: Annotated[str | None, Query()] = None,
    result: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    """Render the audit ledger — every refusal, configuration change and publish, newest first."""
    _require_roles(
        request,
        session,
        web,
        AUDIT_READ_ROLES,
        resource_type="audit",
        message="审计日志仅对管理员开放",
    )
    page_result = list_audit_logs(
        session,
        actor_id=actor_id,
        action=action or None,
        resource_type=resource_type or None,
        result=result or None,
        page=page,
        page_size=LIST_PAGE_SIZE,
    )
    return render(
        request,
        "audit.html",
        {
            "current_user": _identity(web.user),
            "page": page_result,
            # The column is binary; the API has one function that turns it into the usual dotted
            # form, and the page uses the same one rather than formatting `b'...'` by accident.
            "ip_addresses": {
                int(entry.id): format_ip_address(entry.ip_address) for entry in page_result.items
            },
            "actor_filter": actor_id,
            "action_filter": action or "",
            "resource_filter": resource_type or "",
            "result_filter": result or "",
            "result_options": AUDIT_RESULTS,
            "active": "audit",
        },
    )


# --------------------------------------------------------------------------------------
# Topic configuration
# --------------------------------------------------------------------------------------
#
# This is the one settings surface the platform had none of. `pipeline/daily.py` runs the enabled
# topics and nothing else, so with no way to add one the whole pipeline produces nothing and a UI
# on top of it shows an empty shell however good the UI is. `docs/api-contract.md` §3 recorded the
# missing API as "designed but not done"; T19 built that API, and this is its UI face.
#
# What stays off this page is the same thing that stays off the API: triggering a research run.
# A run spends model budget, and that trigger belongs to the scheduler and the operator CLI.

#: Post-redirect-get notices: the action name travels in the URL and the sentence is looked up
#: here. Echoing a free-text message through a query parameter would let anyone craft a link that
#: puts words in the application's mouth.
TOPIC_NOTICES: dict[str, str] = {
    "created": "已创建主题。",
    "updated": "已保存主题。",
    "deleted": "已删除主题，并解除了它的候选关联。",
    "enabled": "已启用该主题，下一次采集会带上它。",
    "disabled": "已停用该主题，下一次采集不会运行它。",
}

#: Which HTTP status a refusal is answered with. The same split the JSON API makes, so a client
#: reading the status sees the same meaning on both surfaces.
TOPIC_ERROR_STATUS: dict[str, int] = {
    "TOPIC_NAME_CONFLICT": status.HTTP_409_CONFLICT,
    "TOPIC_IN_USE": status.HTTP_409_CONFLICT,
    "TOPIC_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "TOPIC_NAME_BLANK": status.HTTP_400_BAD_REQUEST,
}

#: The refusal, said in the language of the page. The service layer's own messages are English and
#: written for a log line or an API client — right for `str(exc)` in a traceback, wrong as the only
#: sentence an operator sees. Unmapped codes fall through to `str(exc)` rather than to a generic
#: "操作失败": a new refusal should appear as itself, obviously unlabelled, and not as a placard
#: that hides what went wrong.
TOPIC_ERROR_MESSAGES: dict[str, str] = {
    "TOPIC_NAME_BLANK": "主题名不能为空。",
    "TOPIC_NAME_CONFLICT": "这个主题名已经被占用，换一个再试。",
    "TOPIC_NOT_FOUND": "主题不存在，可能已经被删掉了。",
    "TOPIC_IN_USE": "这个主题还有历史记录引用它，不能删除",
}


def _topic_refusal(exc: TopicError, *, session: Session, topic_id: int | None) -> str:
    """Render a domain refusal as the sentence the page shows.

    `TOPIC_IN_USE` is the one that keeps its detail, and the dates come from
    `reference_counts` rather than being parsed out of the English message: "which table is
    holding it" is the actionable half of that refusal, and reading it back out of a sentence
    would break the moment somebody rewords the sentence.
    """
    lead = TOPIC_ERROR_MESSAGES.get(exc.code)
    if lead is None:
        return str(exc)
    if exc.code != "TOPIC_IN_USE" or topic_id is None:
        return lead
    counts = reference_counts(session, topic_id)
    blocking = "、".join(f"{name} {count} 条" for name, count in counts.items() if count)
    return f"{lead}：{blocking}。可以改为停用它。" if blocking else lead


class _FormInvalid(Exception):
    """A submitted value the service layer would reject, caught before the call.

    The form sends everything as text, so "eight" in a numeric field has to be turned into a
    message a person can act on rather than a 422 with a JSON pointer in it.
    """


def _split_terms(raw: str) -> list[str] | None:
    """Turn a comma-separated form field into the list the service expects.

    An empty field means "no restriction", which is stored as SQL NULL — so this returns `None`,
    not `[]`. An empty list would mean "restrict to nothing" and select no repositories at all,
    which is the opposite of what a blank field is asking for.
    """
    terms = [part.strip() for part in raw.split(",") if part.strip()]
    return terms or None


#: The fields the topic form carries, in the order they are shown.
TOPIC_FORM_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "publish_mode",
    "languages",
    "keywords",
    "min_stars",
    "window_days",
    "max_candidates",
    "enabled",
)

#: What a brand-new topic's form starts from. These are the column defaults, so a topic created
#: without touching them is the row the pipeline would have produced on its own — not a second
#: definition of "a new topic".
TOPIC_FORM_DEFAULTS: dict[str, str] = {
    "name": "",
    "description": "",
    "publish_mode": "MANUAL",
    "languages": "",
    "keywords": "",
    "min_stars": "0",
    "window_days": "7",
    "max_candidates": "30",
    "enabled": "true",
}


def _topic_form_fields(topic: Topic | None, form: Mapping[str, str] | None) -> dict[str, str]:
    """Return the values the topic form shows, as text.

    Three sources, in that order, on purpose. After a refusal the operator's own input has to
    survive, or they retype nine fields to fix one; on a first open the row is the truth; and for
    a new topic there is no row, so the column defaults above stand in.
    """
    if form:
        return {field: str(form.get(field, "")) for field in TOPIC_FORM_FIELDS}
    if topic is None:
        return dict(TOPIC_FORM_DEFAULTS)
    return {
        "name": topic.name,
        "description": topic.description or "",
        "publish_mode": topic.publish_mode,
        "languages": ", ".join(topic.languages_json or []),
        "keywords": ", ".join(topic.keywords_json or []),
        "min_stars": str(topic.min_stars),
        "window_days": str(topic.window_days),
        "max_candidates": str(topic.max_candidates),
        "enabled": "true" if topic.enabled else "false",
    }


def _topic_values(raw: Mapping[str, str]) -> dict[str, Any]:
    """Translate the create/update form into the service's field values."""
    values: dict[str, Any] = {
        "publish_mode": (raw.get("publish_mode") or "").strip(),
        "description": (raw.get("description") or "").strip() or None,
        "languages": _split_terms(raw.get("languages") or ""),
        "keywords": _split_terms(raw.get("keywords") or ""),
        "enabled": (raw.get("enabled") or "true").strip().lower() in {"true", "1", "on", "yes"},
    }
    for field, label in (
        ("min_stars", "最小星标数"),
        ("window_days", "观察窗口天数"),
        ("max_candidates", "候选上限"),
    ):
        text = (raw.get(field) or "").strip()
        if text == "":
            raise _FormInvalid(f"{label}不能为空")
        try:
            values[field] = int(text)
        except ValueError:
            raise _FormInvalid(f"{label}必须是整数") from None
    return values


def _topics_context(
    request: Request,
    session: Session,
    web: WebSession,
    *,
    query: str | None,
    enabled: str | None,
    page: int,
    notice: str | None = None,
    error: str | None = None,
    form: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build the topic page context, shared by the list route and the write actions' failures.

    A failed write re-renders this page instead of redirecting with the message in the URL: the
    sentence then comes from our own exception rather than from whoever wrote the link, and the
    operator keeps what they typed.
    """
    enabled_filter = {"true": True, "false": False}.get((enabled or "").lower())
    return {
        "current_user": _identity(web.user),
        "page": list_topics(
            session,
            user=web.user,
            enabled=enabled_filter,
            query=query or None,
            page=page,
            page_size=LIST_PAGE_SIZE,
        ),
        "query": query or "",
        "enabled_filter": enabled or "",
        "can_write": not get_role_codes(web.user).isdisjoint(TOPIC_WRITE_ROLES),
        "publish_modes": TOPIC_PUBLISH_MODES,
        "notice": notice,
        "error": error,
        "form": _topic_form_fields(None, form),
        "active": "topics",
    }


def _topics_page_response(
    request: Request,
    session: Session,
    web: WebSession,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render the topic page with a fresh CSRF token available to its forms."""
    return render(
        request,
        "topics.html",
        context,
        status_code=status_code,
        csrf=request.cookies.get(CSRF_COOKIE) or new_csrf_token(),
    )


def _authorize_topic_write(
    request: Request,
    session: Session,
    web: WebSession,
    csrf: str | None,
) -> None:
    """Guard a topic write: CSRF first, then the admin-only role.

    There is no per-row read check the way the report actions have one: visibility for a topic is
    by role, and only `ADMIN` reaches this line — the one role `can_read_topic` lets see everything.
    """
    with _audited_action(request, session, web, resource_type="topic"):
        verify_csrf(request, csrf)
        if get_role_codes(web.user).isdisjoint(TOPIC_WRITE_ROLES):
            raise WebForbidden("主题配置仅对管理员开放")


@router.get("/topics", response_class=HTMLResponse)
def topics_page(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    q: Annotated[str | None, Query()] = None,
    enabled: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    ok: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """Render the topic list — the configuration that decides what the platform collects."""
    _require_roles(
        request,
        session,
        web,
        TOPIC_READ_ROLES,
        resource_type="topic",
        message="当前角色不能查看主题配置",
    )
    context = _topics_context(
        request,
        session,
        web,
        query=q,
        enabled=enabled,
        page=page,
        notice=TOPIC_NOTICES.get((ok or "").strip()),
    )
    return _topics_page_response(request, session, web, context)


@router.get("/topics/{topic_id}", response_class=HTMLResponse)
def topic_page(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    topic_id: int,
    ok: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """Render one topic's edit form.

    Its own page rather than a form folded into the list row: a topic carries nine editable
    fields, and a nine-field form inside a table cell makes the table unreadable and the form
    unusable. The list links here, and this page is where the fields are edited.
    """
    _require_roles(
        request,
        session,
        web,
        TOPIC_READ_ROLES,
        resource_type="topic",
        message="当前角色不能查看主题配置",
    )
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise WebNotFound(f"主题 {topic_id} 不存在")
    if not can_read_topic(web.user, topic, get_role_codes(web.user)):
        raise WebForbidden(f"当前用户无权查看主题 {topic_id}", action="topic.read")
    return render(
        request,
        "topic_detail.html",
        {
            "current_user": _identity(web.user),
            "topic": topic,
            "can_write": not get_role_codes(web.user).isdisjoint(TOPIC_WRITE_ROLES),
            "publish_modes": TOPIC_PUBLISH_MODES,
            "notice": TOPIC_NOTICES.get((ok or "").strip()),
            "error": None,
            "form": _topic_form_fields(topic, None),
            "active": "topics",
        },
        csrf=request.cookies.get(CSRF_COOKIE) or new_csrf_token(),
    )


@router.post("/topics", response_class=HTMLResponse)
def create_topic_action(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    publish_mode: Annotated[str, Form()] = "",
    languages: Annotated[str, Form()] = "",
    keywords: Annotated[str, Form()] = "",
    min_stars: Annotated[str, Form()] = "",
    window_days: Annotated[str, Form()] = "",
    max_candidates: Annotated[str, Form()] = "",
    enabled: Annotated[str, Form()] = "",
    csrf: CsrfField = "",
) -> Response:
    """Create one topic from the configuration page."""
    _authorize_topic_write(request, session, web, csrf)
    raw = {
        "name": name,
        "description": description,
        "publish_mode": publish_mode,
        "languages": languages,
        "keywords": keywords,
        "min_stars": min_stars,
        "window_days": window_days,
        "max_candidates": max_candidates,
        "enabled": enabled,
    }
    try:
        values = _topic_values(raw)
        # `publish_mode` is a keyword of its own on `create_topic`, and the table requires it.
        # Leaving it in `values` would set the same attribute twice.
        mode = str(values.pop("publish_mode"))
        create_topic(
            session,
            actor_id=web.user.id,
            name=name,
            publish_mode=mode,
            values=values,
            request_id=request_id(request),
        )
    except _FormInvalid as exc:
        return _topics_write_failure(
            request, session, web, str(exc), raw, status.HTTP_400_BAD_REQUEST
        )
    except TopicError as exc:
        return _topics_write_failure(
            request,
            session,
            web,
            _topic_refusal(exc, session=session, topic_id=None),
            raw,
            _topic_error_status(exc),
        )
    return RedirectResponse("/ui/topics?ok=created", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/topics/{topic_id}/update", response_class=HTMLResponse)
def update_topic_action(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    topic_id: int,
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    publish_mode: Annotated[str, Form()] = "",
    languages: Annotated[str, Form()] = "",
    keywords: Annotated[str, Form()] = "",
    min_stars: Annotated[str, Form()] = "",
    window_days: Annotated[str, Form()] = "",
    max_candidates: Annotated[str, Form()] = "",
    enabled: Annotated[str, Form()] = "",
    csrf: CsrfField = "",
) -> Response:
    """Save the edit form for one topic.

    The form carries every field, so this is a full replace rather than a patch — which is what
    `update_topic` reports back in `changed`: the fields whose value actually moved.
    """
    _authorize_topic_write(request, session, web, csrf)
    raw = {
        "name": name,
        "description": description,
        "publish_mode": publish_mode,
        "languages": languages,
        "keywords": keywords,
        "min_stars": min_stars,
        "window_days": window_days,
        "max_candidates": max_candidates,
        "enabled": enabled,
    }
    try:
        values = _topic_values(raw)
        values["name"] = name
        update_topic(
            session,
            actor_id=web.user.id,
            topic_id=topic_id,
            values=values,
            request_id=request_id(request),
        )
    except _FormInvalid as exc:
        return _topic_detail_failure(
            request, session, web, topic_id, str(exc), raw, status.HTTP_400_BAD_REQUEST
        )
    except TopicError as exc:
        return _topic_detail_failure(
            request,
            session,
            web,
            topic_id,
            _topic_refusal(exc, session=session, topic_id=topic_id),
            raw,
            _topic_error_status(exc),
        )
    return RedirectResponse(
        f"/ui/topics/{topic_id}?ok=updated",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/topics/{topic_id}/toggle", response_class=HTMLResponse)
def toggle_topic_action(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    topic_id: int,
    enabled: Annotated[str, Form()] = "",
    csrf: CsrfField = "",
) -> Response:
    """Enable or disable one topic without opening the edit form."""
    _authorize_topic_write(request, session, web, csrf)
    turn_on = (enabled or "").strip().lower() in {"true", "1", "on", "yes"}
    try:
        update_topic(
            session,
            actor_id=web.user.id,
            topic_id=topic_id,
            values={"enabled": turn_on},
            request_id=request_id(request),
        )
    except TopicError as exc:
        return _topics_write_failure(
            request,
            session,
            web,
            _topic_refusal(exc, session=session, topic_id=topic_id),
            {},
            _topic_error_status(exc),
        )
    return RedirectResponse(
        f"/ui/topics?ok={'enabled' if turn_on else 'disabled'}"
        f"#{'disabled' if turn_on else 'enabled'}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/topics/{topic_id}/delete", response_class=HTMLResponse)
def delete_topic_action(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    topic_id: int,
    csrf: CsrfField = "",
) -> Response:
    """Delete one topic, or explain which tables still reference it.

    A topic with recorded history cannot be deleted: the foreign keys from reports, runs and
    pipeline rows are `RESTRICT`, and `delete_topic` turns that into a refusal naming the tables
    instead of letting an `IntegrityError` surface as a 500. The message is rendered verbatim
    because it is ours, and "which table" is the one thing the operator needs to act on.
    """
    _authorize_topic_write(request, session, web, csrf)
    try:
        delete_topic(
            session,
            actor_id=web.user.id,
            topic_id=topic_id,
            request_id=request_id(request),
        )
    except TopicError as exc:
        return _topics_write_failure(
            request,
            session,
            web,
            _topic_refusal(exc, session=session, topic_id=topic_id),
            {},
            _topic_error_status(exc),
        )
    return RedirectResponse("/ui/topics?ok=deleted", status_code=status.HTTP_303_SEE_OTHER)


def _topic_error_status(exc: TopicError) -> int:
    """Map a domain refusal onto its HTTP status."""
    return TOPIC_ERROR_STATUS.get(exc.code, status.HTTP_409_CONFLICT)


def _topics_write_failure(
    request: Request,
    session: Session,
    web: WebSession,
    message: str,
    form: Mapping[str, str],
    status_code: int,
) -> HTMLResponse:
    """Re-render the topic list with a refusal and whatever the operator had typed."""
    context = _topics_context(
        request,
        session,
        web,
        query=None,
        enabled=None,
        page=1,
        error=message,
        form=form,
    )
    return _topics_page_response(request, session, web, context, status_code=status_code)


def _topic_detail_failure(
    request: Request,
    session: Session,
    web: WebSession,
    topic_id: int,
    message: str,
    form: Mapping[str, str],
    status_code: int,
) -> HTMLResponse:
    """Re-render one topic's edit form with a refusal and whatever the operator had typed."""
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise WebNotFound(f"主题 {topic_id} 不存在")
    return render(
        request,
        "topic_detail.html",
        {
            "current_user": _identity(web.user),
            "topic": topic,
            "can_write": not get_role_codes(web.user).isdisjoint(TOPIC_WRITE_ROLES),
            "publish_modes": TOPIC_PUBLISH_MODES,
            "notice": None,
            "error": message,
            "form": _topic_form_fields(topic, form),
            "active": "topics",
        },
        status_code=status_code,
        csrf=request.cookies.get(CSRF_COOKIE) or new_csrf_token(),
    )
