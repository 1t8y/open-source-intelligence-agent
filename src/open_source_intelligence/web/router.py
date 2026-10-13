"""Server-rendered admin UI: report list, report detail, and the review workflow.

The pages are rendered with Jinja2 and enhanced with HTMX, which is vendored under
`static/` so the interface works without any outbound network access. Every page reuses the
same service layer as the JSON API; the UI never touches SQL itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Form, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.core.config import Settings
from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.identity import User
from open_source_intelligence.persistence.models.intelligence import Topic
from open_source_intelligence.persistence.models.reporting import Report
from open_source_intelligence.reports import queries
from open_source_intelligence.reports.errors import ReportGenerationError, ReportPublishError
from open_source_intelligence.reports.publishing import (
    publish_due_reports,
    publish_or_request_review,
    publish_report,
    record_review,
)
from open_source_intelligence.reports.revisions import revise_report, rollback_report
from open_source_intelligence.reports.verification import verify_report
from open_source_intelligence.security.audit import record_audit_event
from open_source_intelligence.security.authorization import (
    REPORT_WRITE_ROLES,
    can_read_report,
    readable_reports_filter,
)
from open_source_intelligence.security.dependencies import (
    client_ip,
    get_db_session,
    request_id,
)
from open_source_intelligence.security.service import authenticate_user, get_role_codes
from open_source_intelligence.security.tokens import (
    ACCESS_TOKEN_EXPIRES_SECONDS,
    create_access_token,
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

router = APIRouter(prefix="/ui", tags=["ui"], include_in_schema=False)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

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


def render(
    request: Request,
    name: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
    csrf: str | None = None,
) -> HTMLResponse:
    """Render a template and hand the browser a CSRF cookie when it lacks one."""
    payload: dict[str, Any] = {"csrf_token": csrf or request.cookies.get(CSRF_COOKIE, "")}
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


def _require_write_role(session: WebSession) -> None:
    """Reject a mutating UI action from a read-only role."""
    if get_role_codes(session.user).isdisjoint(REPORT_WRITE_ROLES):
        raise WebForbidden("只读角色不能执行该操作")


def _authorize_action(
    request: Request,
    session: Session,
    web: WebSession,
    report_id: int,
    csrf: str | None,
) -> None:
    """Guard a mutating UI action: CSRF, then write role, then read access.

    The read check runs *before* the service call so an action can never take effect on a
    report the caller may not see.
    """
    verify_csrf(request, csrf)
    _require_write_role(web)
    _load_visible_report(session, web, report_id)


def _load_visible_report(session: Session, web: WebSession, report_id: int) -> Report:
    """Load a report the current user may read, or render the matching error page."""
    report = session.get(Report, report_id)
    if report is None:
        raise WebNotFound(f"报告 {report_id} 不存在")
    if not can_read_report(web.user, report, get_role_codes(web.user)):
        raise WebForbidden("当前用户无权查看该报告")
    return report


def _topic_name(session: Session, topic_id: int) -> str | None:
    """Return one topic name."""
    return session.scalar(select(Topic.name).where(Topic.id == topic_id))


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
def logout(request: Request, csrf: CsrfField) -> Response:
    """Clear the session cookie."""
    verify_csrf(request, csrf)
    response = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


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
    topic_names: dict[int, str] = {}
    if page_result.items:
        rows = session.execute(
            select(Topic.id, Topic.name).where(
                Topic.id.in_({item.topic_id for item in page_result.items})
            )
        ).all()
        topic_names = {int(row[0]): str(row[1]) for row in rows}
    return render(
        request,
        "reports.html",
        {
            "current_user": _identity(web.user),
            "page": page_result,
            "topic_names": topic_names,
            "status_filter": status_filter or "",
            "visibility_filter": visibility or "",
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
