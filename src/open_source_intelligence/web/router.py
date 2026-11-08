"""服务端渲染的管理界面：报告列表、报告详情与审核流程。

页面由 Jinja2 渲染、由 HTMX 增强，HTMX 内置在 `static/` 下，因此界面在完全没有外网
访问时也能工作。每个页面都复用 JSON API 所用的同一层服务；界面自己从不直接碰 SQL。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
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

from open_source_intelligence.agent.errors import AGENT_ERROR_HINTS, AgentRunReviewError
from open_source_intelligence.agent.reviews import (
    REVIEWABLE_RUN_STATUS,
    latest_run_reviews,
    list_run_reviews,
    record_run_review,
)
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
from open_source_intelligence.persistence.models.agent import (
    AGENT_RUN_REVIEW_VERDICTS,
    AGENT_RUN_STATUSES,
)
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
from open_source_intelligence.pipeline.runs import (
    RUN_STATUSES,
    RUN_TYPES,
    latest_publish_decisions,
    list_pipeline_runs,
)
from open_source_intelligence.reports import queries
from open_source_intelligence.reports.errors import ReportGenerationError, ReportPublishError
from open_source_intelligence.reports.publishing import (
    publish_due_reports,
    publish_or_request_review,
    publish_report,
    record_review,
    withdraw_report,
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
from open_source_intelligence.workers.celery_app import (
    DAILY_PIPELINE_TASK,
    RESEARCH_AGENT_TASK,
    create_celery_app,
)

from .markdown import render_markdown
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

#: 看板以按钮形式提供的观察窗口。接口接受 1-90 之间的任意值，其余一律拒绝而不是
#: 悄悄截断；这三个才是运维真正会要的。
WINDOW_OPTIONS = (1, 7, 30)
#: 看板在指向完整账本之前，先列出多少条最近的运行。
DASHBOARD_RUN_PAGE_SIZE = 10
#: 侧边栏外壳内的列表页每页显示多少行。页大小是固定的，不是查询参数：这些是浏览页，
#: 想要别的页大小的人要的是 JSON API。
LIST_PAGE_SIZE = 20
#: 「一屏」页面显示多少条投递记录与账本行。`list_pushes`、`list_jobs` 和
#: `list_eval_runs` 接受的是上限而不是页码，所以页面如实写出这个上限，而不是假装分页。
RECENT_LIMIT = 50
#: 账本显示多少条流水线运行。一次每日流水线会为每个阶段各写一条运行（约 8 条），
#: 因此 50 行略多于一周的历史：足够从头到尾读完一条链路，又不至于把卡片变成第二个
#: 数据库浏览器。类型与状态筛选可以在此基础上再收窄。
PIPELINE_RECENT_LIMIT = 50
#: Agent 运行账本对 `?review=` 接受的取值。其它值一律忽略而不是往下传，因为 URL 里
#: 打错字不该让一个只读页面报错。
RUN_REVIEW_FILTERS = ("open", "done")

#: 有权在界面上看到**运维配置**类信息的角色。目前指的是推送目标白名单，以及那些
#: 提示读者「去建一个」的空状态文案。
#:
#: 这些配置刻意不由控制台来管。推送目标决定一份已发布的报告可以走哪些出口，账号
#: 则由引导 CLI 创建；两者都属于贴近部署的配置，所以归后端所有，界面只展示状态。
#: 当状态无法回避时 ——「你还不能推送」总要说清原因 —— 解释只渲染给管理员看，
#: 因为只有管理员能据此行动。`AUDIT_READ_ROLES` 与 `TOPIC_WRITE_ROLES` 出于同样的
#: 理由也收在同一个角色上：它们改变的是平台的行为，而不是某一份报告的内容。
OPS_CONFIG_ROLES: Final[tuple[str, ...]] = (ROLE_ADMIN,)

#: 侧边栏，按显示顺序排列：`(组标题, ((key, label, path, roles), ...))`。
#: 刻意声明在路由旁边 —— 条目的 `roles` 就是它的路由所强制的同一组角色，因此不会
#: 出现「某个身份能看到某个页面、打开时却被拒绝」的情况。
NAV_GROUPS: tuple[tuple[str, tuple[tuple[str, str, str, tuple[str, ...]], ...]], ...] = (
    (
        "总览",
        (("home", "首页", "/ui/home", REPORT_READ_ROLES),),
    ),
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
    """Jinja 过滤器：把日志脱敏规则应用到单个自由文本值上。"""
    return None if value is None else redact_text(str(value))


def _redacted_json(value: object) -> str:
    """Jinja 过滤器：序列化 JSON 列用于展示，并抹掉其中的凭据。

    运行轨迹既由界面渲染，也由 JSON API 提供，两者都是数据出口。复用 `redact_text`
    让「什么可以离开进程」只有一个定义，而不是让页面把接口刻意隐藏的内容显示出来。
    """
    return redact_text(json.dumps(value, ensure_ascii=False, default=str))


templates.env.filters["redact"] = _redact
templates.env.filters["redacted_json"] = _redacted_json


#: 平台存储的枚举码对应的展示标签。**只翻译标签 —— 取值仍是契约里的码。** 下拉框
#: 提交的是它的 `value`，所以一个取值被翻成「待审核」的选项会发出服务端并不存在的
#: 状态；徽章上显示的码与表单里提交的码之间，同样是这条界线。
#:
#: 标识符刻意不做映射、原样透传：错误码（`AGENT_EVIDENCE_INCOMPLETE`）、审计动作名
#: （`topic.create`）、评测目标名（`agent_full`）与角色码（`ADMIN`）都是运维要在契约
#: 里 grep 的东西，翻译过的副本只会更难对上。
ENUM_LABELS: dict[str, str] = {
    # 报告生命周期。
    "draft": "草稿",
    "pending_review": "待审核",
    "changes_requested": "退回修改",
    "approved": "已批准",
    "scheduled": "待发布",
    "published": "已发布",
    # 运行与任务生命周期。`pending`/`failed` 被其中几台状态机共用。
    "pending": "待处理",
    "queued": "排队中",
    "running": "运行中",
    "succeeded": "成功",
    "success": "成功",
    "failed": "失败",
    "partial": "部分成功",
    "completed": "已完成",
    "cancelled": "已取消",
    # 仅用于 Agent 运行：运行结束了，但证据基础不足以支撑一份草稿，得让人看一眼。
    # 措辞是对这次运行的判断，而不是一个待办项 —— 根本不存在「确认这次运行」这个
    # 操作，旧标签却许诺了它。
    "needs_review": "需人工复核",
    # 复核人得出的结论。不是 approve/reject：这里没有草稿可供批准，只有「这段解释
    # 能否结案，还是需要再试一次」的判断。
    "acknowledged": "已确认原因",
    "needs_rerun": "要重跑",
    "denied": "被拒",
    "sent": "已送达",
    "passed": "通过",
    # 流水线运行类型：每个类型对应链路中的一个阶段。`agent`/`publish`/`push` 按主题
    # 运行，其余类型在一轮里对所有已启用主题运行。
    "collect": "采集",
    "snapshot": "快照",
    "rank": "排名",
    "contents": "正文缓存",
    "backfill": "回填",
    "agent": "研究",
    "publish": "发布",
    "push": "推送",
    # 可见性、敏感性，以及两种发布模式（报告与主题共用这套词汇）。
    "PRIVATE": "私有",
    "TEAM": "团队",
    "INTERNAL": "内部",
    "PUBLIC": "公开",
    "CONFIDENTIAL": "机密",
    "AUTO": "自动",
    "AUTO_WITH_DELAY": "延迟自动",
    "MANUAL": "手动",
    # Agent 步骤轨迹。`step_type` 是实际运行的图节点，`status` 是它的判定；两者在
    # 运行页上成对打印（`规划 · 正常`），毕竟原始码读起来像调用栈帧。
    "plan": "规划",
    "tool": "调用工具",
    "observe": "整理证据",
    "verify": "验证",
    "write": "成稿",
    "ok": "正常",
    "insufficient": "证据不足",
    "skipped": "跳过",
    "degraded": "降级",
    # 投递渠道。
    "email": "邮件",
    "webhook": "Webhook",
    # 人工审核决策。审计链保留原始码（审计行记录的是操作名，而不是它的标签）；
    # 这三条之所以存在，是因为报告页会把决策打印在复核人旁边，而中文表格里的
    # 「approve」读起来像泄漏出来的字段名。
    "approve": "通过",
    "reject": "拒绝",
    "request_changes": "退回修改",
    # 版本存在的原因。`rollback_to_vN` 是连版本号一起生成的，所以由 `_label` 里的
    # 前缀规则处理，而不是在这里写一条固定条目。
    "generated_from_agent_draft": "由 Agent 草稿生成",
    "manual_edit": "人工编辑",
}


def _label(value: object) -> str:
    """把一个枚举码翻译成展示文案，映射不到时回退成码本身。

    回退很重要：未映射的值显示为 `agent_full` 而不是什么都不显示，于是模型里新增的
    状态会以它自己的码出现在页面上，一眼就能看出「尚未打标签」，而不是一个空白单元格。
    """
    text = "" if value is None else str(value)
    if text.startswith("rollback_to_v"):
        # 有一条原因把目标版本编码在取值里（`revisions.py` 写的是
        # `rollback_to_v{version}`），所以只能用规则翻译。数字刻意原样透传：
        # 它是版本号，不是一个词。
        return f"回滚到 v{text.removeprefix('rollback_to_v')}"
    return ENUM_LABELS.get(text, text)


#: 发布策略的原因码是给运维看的裁决依据，不是字段泄漏。界面上要把它翻成一句能照着做的
#: 话，否则「待审核」旁边只有 `FIRST_RUN`，用户既不知道为什么没发，也不知道去哪儿处理。
#: 键与 `reports/publishing.py` 的 `REASON_*` 一一对应；未映射的码原样透传。
PUBLISH_REASON_LABELS: dict[str, str] = {
    "FIRST_RUN": "该主题首份报告，需要人工审核放行",
    "AGENT_RUN_NOT_CLEAN": "研究运行未干净完成（证据不足或报错）",
    "VERIFICATION_FAILED": "当前版本验证未通过",
    "SECURITY_RISK": "检测到提示词注入等安全风险",
    "SENSITIVE_REPORT": "报告为机密级，需同版本人工审核",
    "MODEL_OR_PROMPT_CHANGED": "模型或提示词版本相对上期发生变化",
    "NOT_INTERNAL_VISIBILITY": "可见范围不是内部，需人工确认",
    "LOW_CITATION_COVERAGE": "引用覆盖率不达标",
    "SENSITIVE_CONTENT": "正文含敏感内容",
}


def _publish_reasons(codes: Sequence[str]) -> list[str]:
    """把原因码翻成可展示的中文短句；未知码保留原值。"""
    return [PUBLISH_REASON_LABELS.get(code, code) for code in codes]


def _decision_context(session: Session, report_ids: Sequence[int]) -> dict[str, Any]:
    """给列表页准备「每份报告为什么没发」的可展示数据。

    键是 report_id，值同时带原始裁决和中文原因，模板不需要再认识 `metrics_json` 的结构。
    """
    decisions = latest_publish_decisions(session, report_ids=report_ids)
    return {
        "publish_decisions": {
            report_id: {
                "run_id": decision.run_id,
                "resolution": decision.resolution,
                "reason_codes": decision.reason_codes,
                "reasons": _publish_reasons(decision.reason_codes),
            }
            for report_id, decision in decisions.items()
        }
    }


templates.env.filters["label"] = _label
# 报告正文是 Markdown。该过滤器返回的是已净化标记，模板不能在其上再加
# `| safe`：转义正是它存在的意义（见 `web/markdown.py`）。
templates.env.filters["markdown"] = render_markdown

DbSession = Annotated[Session, Depends(get_db_session)]
#: 别名很关键：模板提交的令牌字段名是 `csrf_token`，而 FastAPI 默认按声明名绑定
#: 表单参数。再配合每个使用点上的 `= ""` 默认值，漏带令牌的表单仍会走到
#: `verify_csrf`、拿到渲染好的 403 页面，而不是一个 JSON 校验错误。
CsrfField = Annotated[str, Form(alias=CSRF_FIELD)]


def get_web_session(request: Request, session: DbSession) -> WebSession:
    """包在 `load_web_session` 外面的 FastAPI 依赖。"""
    return load_web_session(request, session)


WebSessionDep = Annotated[WebSession, Depends(get_web_session)]


def add_web_surface(app: FastAPI) -> None:
    """挂载静态资源，并注册界面用的重定向与拒绝响应。"""
    app.mount("/ui/static", StaticFiles(directory=str(STATIC_DIR)), name="ui-static")

    @app.exception_handler(WebRedirect)
    async def redirect_handler(_: Request, exc: WebRedirect) -> Response:
        return RedirectResponse(exc.location, status_code=exc.status_code)

    @app.exception_handler(WebForbidden)
    async def forbidden_handler(request: Request, exc: WebForbidden) -> Response:
        # htmx 交换进来的拒绝落在一个小容器里。整张 403 页面会被嵌进卡片内部 ——
        # 正是触发片段要避免的那个毛病 —— 所以交换请求只拿一行提示，状态码照旧保留。
        if _is_htmx(request):
            return templates.TemplateResponse(
                request,
                "partials/alert.html",
                {"message": exc.message, "current_user": None, "csrf_token": ""},
                status_code=status.HTTP_403_FORBIDDEN,
            )
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
    """返回绑定在运行中应用上的配置。"""
    settings = request.app.state.settings
    assert isinstance(settings, Settings)
    return settings


def _identity(user: User) -> dict[str, Any]:
    """把已登录用户整理成便于渲染的身份信息。"""
    return {"username": user.username, "roles": sorted(get_role_codes(user))}


def _roles_of(context: dict[str, Any]) -> frozenset[str]:
    """读取页面已持有的身份所携带的角色码。

    `context["current_user"]` 只由这一个函数读取，因为有三个调用方要问它同一个问题：
    画哪些侧边栏条目、要不要画看板条目、这个身份能看哪些运维界面。若每条规则都自己
    去读这个字典，就会多出第四种「带角色登录」的解释，四者迟早会走偏。
    """
    identity = context.get("current_user")
    if not isinstance(identity, dict):
        return frozenset()
    declared = identity.get("roles")
    if not isinstance(declared, (list, tuple, set)):
        return frozenset()
    return frozenset(str(role) for role in declared)


def _has_role(context: dict[str, Any], allowed: tuple[str, ...]) -> bool:
    """返回页面的身份是否与 `allowed` 有交集。

    写成求交集而不是 `in`：今天只由一个角色把关的规则，在元组变长后仍会默默生效，
    而 `role in allowed` 就不会。
    """
    return not _roles_of(context).isdisjoint(allowed)


def _sees_ops_config(web: WebSession) -> bool:
    """返回该会话能否看到运维配置类界面。

    它是渲染器从身份推出的 `can_manage_ops` 在实体层面的孪生。路由有时必须在模板
    存在*之前*就做决定 —— 比如到底要不要加载推送目标白名单 —— 此时它没法去问渲染器。
    两者都读 `OPS_CONFIG_ROLES`，所以页面携带的数据和展示它的卡片不会各说各话。
    """
    return not get_role_codes(web.user).isdisjoint(OPS_CONFIG_ROLES)


def _navigation(context: dict[str, Any]) -> list[dict[str, Any]]:
    """为页面已携带的身份构建侧边栏。

    从 `context["current_user"]` 推导，理由与 `can_manage_ops` 相同：每个模板都要渲染
    导航，它们都不该自己去算谁能看什么。身份缺少某个组的角色时整个条目被丢掉，组内
    条目全空时连标题也一起丢掉 —— 一个空组标题等于承诺了一个并不存在的页面。
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
            # 这个列表的键叫 `entries` 而不是 `items`：Jinja 在字典上取 `group.items`
            # 时会先解析到内置的绑定方法，根本轮不到那个键，于是
            # `{% for x in group.items %}` 抛出 "not iterable"。改个名，比留一条下个
            # 模板没人记得的规则便宜。
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
    """渲染模板；浏览器没有 CSRF Cookie 时顺手发一个。"""
    payload: dict[str, Any] = {
        "csrf_token": csrf or request.cookies.get(CSRF_COOKIE, ""),
        # 把从状态机推导出的集合交给模板，而不是在模板里重抄一遍，这样页面给出的按钮
        # 与服务接受的状态不会脱节。
        "editable_statuses": sorted(EDITABLE_STATUSES),
        "rollbackable_statuses": sorted(ROLLBACKABLE_STATUSES),
        # 侧边栏与当前条目：每条规则只在一处判定，且在这里算好，模板无需知道哪个角色
        # 能看哪个页面。
        "navigation": _navigation(context),
        # 这个身份能看哪些运维界面。理由与侧边栏相同，形状也相同，于是渲染运维详情的
        # 页面不会比提供它的路由更宽松。
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
    """在拒绝被渲染出来之前，先把这次界面拒绝记进审计。

    JSON API 对每次拒绝都写一行审计；界面渲染了同样的拒绝却什么都没写，审计链于是
    只留下 API 那一半故事。`docs/security-and-privacy.md` §3.1.1 要求把审计落在*写*
    上，而不是落在传输上，所以界面也必须记录。

    记录放在这里而不是 `WebForbidden` 处理器里，是因为处理器运行在请求的依赖图之外，
    手上没有可以写入的数据库会话。
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
    """拒绝只读角色发起的写类界面操作。"""
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
    """拒绝页面角色未覆盖的身份，并把这次拒绝记录下来。

    所有不向所有人开放的页面共用这一个辅助函数：它们的审计动作、资源类型和拒绝路径
    都相同；若某个页面自己写一份，它的拒绝就会以别的名字归档，和其它页面对不上。
    """
    with _audited_action(request, session, web, resource_type=resource_type):
        if get_role_codes(web.user).isdisjoint(allowed):
            raise WebForbidden(message)


def _require_metrics_role(request: Request, session: Session, web: WebSession) -> None:
    """把普通查看者挡在运维看板之外。

    看板暴露的是跨主题的成本、失败原因与评测结论，而不是某一份报告的内容，所以它
    遵循 `METRICS_ROLES`，而不是报告读取角色。
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
    """为一个写类界面操作把关：先 CSRF，再写角色，最后读权限。

    读权限检查发生在调用服务*之前*，因此任何操作都不可能作用在调用者看不到的报告上；
    三种拒绝中的任何一种都会在抛出的同时记入审计。
    """
    with _audited_action(request, session, web, resource_type="report", resource_id=str(report_id)):
        verify_csrf(request, csrf)
        _require_write_role(web)
        _load_visible_report(session, web, report_id)


def _load_visible_report(session: Session, web: WebSession, report_id: int) -> Report:
    """加载当前用户有权读取的报告，否则渲染对应的错误页。

    拒绝会以 `report.read` 归档，与 JSON API 记录的是同一个动作，于是「谁试图访问哪份
    报告」只需按一个动作名查一次，而不是查两次。
    """
    report = session.get(Report, report_id)
    if report is None:
        raise WebNotFound(f"报告 {report_id} 不存在")
    if not can_read_report(web.user, report, get_role_codes(web.user)):
        raise WebForbidden("当前用户无权查看该报告", action="report.read")
    return report


def _topic_name(session: Session, topic_id: int) -> str | None:
    """返回单个主题的名称。"""
    return session.scalar(select(Topic.name).where(Topic.id == topic_id))


def _topic_names(session: Session, topic_ids: set[int]) -> dict[int, str]:
    """为列表页建立主题 id 到名称的映射。

    这只是展示用的名称查询，不是读模型：列表查询返回的行只带一个 `topic_id`，而页面
    得印出人能认出来的东西。形状与 `reports_page` 内联做的那次查询相同 —— 抽到这里，
    是为了让仓库页与主题页共用这一份，而不是各自发明一份。
    """
    if not topic_ids:
        return {}
    rows = session.execute(select(Topic.id, Topic.name).where(Topic.id.in_(topic_ids))).all()
    return {int(row[0]): str(row[1]) for row in rows}


def _report_titles(session: Session, report_ids: set[int]) -> dict[int, str]:
    """为列表页建立报告 id 到标题的映射。参见 `_topic_names`。"""
    if not report_ids:
        return {}
    rows = session.execute(select(Report.id, Report.title).where(Report.id.in_(report_ids))).all()
    return {int(row[0]): str(row[1]) for row in rows}


def _push_context(session: Session, report_id: int) -> dict[str, Any]:
    """构建推送卡片需要的数据：白名单，以及这份报告的投递轨迹。"""
    return {
        "push_targets": list_targets(session),
        "pushes": list_pushes(session, report_id=report_id, limit=20),
        "push_statuses": sorted(PUSHABLE_STATUSES),
    }


def _research_trace_context(session: Session, report: Report) -> dict[str, Any]:
    """构建折叠起来的「研究轨迹」卡片所需的数据。

    轨迹是 Agent 的逐步执行路径（它计划了什么、调用了哪些工具、每个工具返回了
    什么）。在报告页上它刻意是*折叠*的：报告的价值在成稿的叙述，不在机器内部，所以
    轨迹可以按需查看，而不必把它的耗时和 token 列推到读者眼前。
    """
    trace = (
        get_agent_run_trace(session, run_id=report.agent_run_id) if report.agent_run_id else None
    )
    return {"trace": trace}


def _runtime_mode(settings: Settings) -> dict[str, Any]:
    """描述这次部署是怎么接的，让页面能区分「离线演示」与「真实运行」。

    决定平台是否真的在跑的三件事 —— 模型供应商、投递通道、研究是否自动派发 —— 被放
    在一起呈现，读者就不会把离线桩产品误当成真货。只有确定性桩会让 `provider` 取
    "offline"；任何远程供应商都算真。
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
    """描述投递是怎么配置的，好让账本页把它明说出来。

    答案就是服务实际依据的那三件事 —— `push_delivery`、推送是否开启、Webhook 通道
    是否带签名密钥 —— 外加本地演练才会拨动的两个仅测试用逃生口。读者不该靠一张满是
    `offline:` 细节的表，自己去推断「这些回执全是离线的」。
    """
    return {
        "delivery": settings.push_delivery,
        "enabled": settings.push_enabled,
        "webhook_signed": settings.push_webhook_signing_secret is not None,
        "allow_private_targets": settings.push_webhook_allow_private_targets,
        "custom_ca": settings.push_webhook_ca_bundle is not None,
    }


def _share_context(session: Session, report: Report) -> dict[str, Any]:
    """构建分享卡片需要的数据：已有的链接，以及能否再生成一条。

    可否分享的判断用的是服务拒绝时依据的同样两个常量（`SHAREABLE_SENSITIVITY`、
    `SHAREABLE_STATUS`），所以卡片不会给出一个服务必然拒绝的按钮 —— 与导航和主题页
    遵循的是同一条规则。
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
# 首页
# --------------------------------------------------------------------------------------

#: 允许从首页触发采集或研究运行的角色。研究运行要花真实的模型预算，采集要消耗
#: 公共 API 配额，所以两者都交给写角色而不是所有读者；成本写在按钮旁边的卡片上。
TRIGGER_ROLES: Final[tuple[str, ...]] = REPORT_WRITE_ROLES

#: 首页两个触发动作的审计动作名。`collector.run` 已经存在（采集阶段在记录它）；
#: 从界面派发研究是一块新入口，所以它在 `report.*`、`topic.*` 家族之外单列一个
#: `agent.run`。
AUDIT_COLLECT: Final[str] = "collector.run"
AUDIT_RESEARCH: Final[str] = "agent.run"


@router.get("/home", response_class=HTMLResponse)
def home_page(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    now: Annotated[datetime, Depends(utc_now)],
) -> HTMLResponse:
    """渲染落地页：系统状态、最新报告，以及触发入口。

    这是已登录用户看到的第一个页面，所以它要回答 D3 验收点名的三个问题 —— 系统在做
    什么、最新的一份报告是哪份、怎么触发一次 —— 而不用把读者打发去账本。它和其它
    页面一样通过同一层服务读取数据；这里没有任何地方直接发 SQL。
    """
    roles = get_role_codes(web.user)
    overview = collect_metrics(
        session,
        now=now,
        window_days=DEFAULT_WINDOW_DAYS,
        settings=_settings(request),
    )
    reports = queries.list_reports(
        session,
        access_filter=readable_reports_filter(web.user, roles),
        page=1,
        page_size=1,
    )
    newest = reports.items[0] if reports.items else None
    topic_names = _topic_names(session, {newest.topic_id} if newest is not None else set())
    can_trigger = not roles.isdisjoint(TRIGGER_ROLES)
    topics = list_topics(session, user=web.user, enabled=True, page_size=TOPIC_MAX_PAGE_SIZE)
    return render(
        request,
        "home.html",
        {
            "current_user": _identity(web.user),
            "overview": overview,
            "newest": newest,
            "newest_topic_name": topic_names.get(newest.topic_id) if newest is not None else None,
            "can_trigger": can_trigger,
            "topics": topics.items,
            "collect_limit": _settings(request).pipeline_collect_limit,
            "runtime_mode": _runtime_mode(_settings(request)),
            "active": "home",
        },
        csrf=request.cookies.get(CSRF_COOKIE) or new_csrf_token(),
    )


@router.post("/home/collect", response_class=HTMLResponse)
def home_collect(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    csrf: CsrfField = "",
) -> Response:
    """在 worker 上排入一次完整的每日流水线（采集 → 快照 → 排名 → 正文缓存）。

    运行是异步派发的：采集要打公共 API，可能耗时数分钟，所以请求只返回一条提示，
    活儿交给 worker 干。只有写角色可以消耗配额，派发本身会记入审计。
    """
    verify_csrf(request, csrf)
    _require_roles(
        request,
        session,
        web,
        TRIGGER_ROLES,
        resource_type="pipeline",
        message="只有管理员或编辑可以触发采集",
    )
    settings = _settings(request)
    task_id = _queue_collect(settings)
    record_audit_event(
        session,
        action=AUDIT_COLLECT,
        resource_type="pipeline",
        result="success",
        actor_id=web.user.id,
        request_id=request_id(request),
        ip_address=client_ip(request),
        metadata_json={"task_id": task_id, "provider": "all", "source": "ui"},
    )
    session.commit()
    return _trigger_response(
        request,
        session,
        web,
        result=_trigger_result(
            ok=True,
            title="已排队采集任务",
            task_id=task_id,
            detail="采集 → 快照 → 排名 → 正文缓存依次在 worker 上进行。",
            href="/ui/runs",
            link_label="采集与运行",
        ),
    )


@router.post("/home/research", response_class=HTMLResponse)
def home_research(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    topic_id: Annotated[int, Form()],
    csrf: CsrfField = "",
) -> Response:
    """为某个主题在 worker 上排入一次研究运行。

    研究运行要花真实的模型预算，因此该端点强制写角色、校验主题存在且已启用，并把
    派发记进审计。运行本身是异步的。
    """
    verify_csrf(request, csrf)
    _require_roles(
        request,
        session,
        web,
        TRIGGER_ROLES,
        resource_type="agent",
        message="只有管理员或编辑可以触发研究",
    )
    topic = session.get(Topic, topic_id)
    if topic is None or not topic.enabled:
        record_audit_event(
            session,
            action=AUDIT_RESEARCH,
            resource_type="agent",
            resource_id=str(topic_id),
            result="denied",
            actor_id=web.user.id,
            request_id=request_id(request),
            ip_address=client_ip(request),
            metadata_json={"reason": "topic missing or disabled"},
        )
        session.commit()
        return _trigger_response(
            request,
            session,
            web,
            result=_trigger_result(
                ok=False,
                title="无法触发研究",
                detail=f"主题 {topic_id} 不存在或已停用，先到「主题管理」确认。",
                href="/ui/topics",
                link_label="主题管理",
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    task_id = _queue_research(_settings(request), topic_id=topic_id)
    record_audit_event(
        session,
        action=AUDIT_RESEARCH,
        resource_type="agent",
        resource_id=str(topic_id),
        result="success",
        actor_id=web.user.id,
        request_id=request_id(request),
        ip_address=client_ip(request),
        metadata_json={"task_id": task_id, "topic_id": topic_id, "source": "ui"},
    )
    session.commit()
    return _trigger_response(
        request,
        session,
        web,
        result=_trigger_result(
            ok=True,
            title="已排队研究任务",
            task_id=task_id,
            detail=f"主题 {topic.name}；新稿落到「报告中心」等待审核。",
            href="/ui/reports",
            link_label="报告中心",
        ),
    )


def _queue_collect(settings: Settings) -> str:
    """在 broker 上排入一次完整的每日流水线，返回 Celery 任务 id。"""
    app = create_celery_app(settings)
    result = app.send_task(
        DAILY_PIPELINE_TASK,
        kwargs={"provider": "all", "limit": settings.pipeline_collect_limit},
    )
    return str(result.id)


def _queue_research(settings: Settings, *, topic_id: int) -> str:
    """在 broker 上排入一次研究运行，返回 Celery 任务 id。"""
    app = create_celery_app(settings)
    result = app.send_task(RESEARCH_AGENT_TASK, kwargs={"topic_id": topic_id})
    return str(result.id)


def _is_htmx(request: Request) -> bool:
    """返回该请求是否来自 htmx。

    htmx 会在它发出的每个请求上盖 `HX-Request: true`。要把内容交换进小容器的处理器
    需要这个信号：同一个端点同时还要服务没有 JavaScript 的浏览器，而两类受众里只有
    一类能接受片段。
    """
    return request.headers.get("HX-Request", "").lower() == "true"


def _trigger_result(
    *,
    ok: bool,
    title: str,
    task_id: str | None = None,
    detail: str | None = None,
    href: str | None = None,
    link_label: str | None = None,
) -> dict[str, Any]:
    """把一次触发的结果只描述一遍，供片段与兜底页共用。"""
    return {
        "ok": ok,
        "title": title,
        "task_id": task_id,
        "detail": detail,
        "href": href,
        "link_label": link_label,
    }


def _trigger_response(
    request: Request,
    session: Session,
    web: WebSession,
    *,
    result: dict[str, Any],
    status_code: int = status.HTTP_200_OK,
) -> Response:
    """用片段回应 htmx 的首页触发，用整页回应普通表单提交。

    两个触发表单都指向 `#home-trigger-result` 并带 `hx-swap="innerHTML"`，所以一份
    完整文档会被插进卡片*内部* —— 外壳套在自己里面，既不好读也不好看。片段是 htmx
    交换唯一能接受的东西。

    同一批表单仍然带 `method`/`action`，因此没有 JavaScript 的浏览器也会提交它们。
    这类客户端拿到的是整页，消息以提示条的形式出现 —— 这也是为什么结果只在
    `_trigger_result` 里描述一遍，而不是每种响应形态各写一遍。
    """
    if _is_htmx(request):
        return render(
            request,
            "partials/home_trigger_result.html",
            {"result": result},
            status_code=status_code,
        )
    message = result["title"]
    if result["task_id"]:
        message = f"{message} {result['task_id'][:8]}…"
    return render(
        request,
        "home.html",
        _home_notice_context(
            session,
            web,
            request,
            now=None,
            notice=message if result["ok"] else None,
            error=None if result["ok"] else message,
        ),
        status_code=status_code,
        csrf=request.cookies.get(CSRF_COOKIE) or new_csrf_token(),
    )


def _home_notice_context(
    session: Session,
    web: WebSession,
    request: Request,
    *,
    now: datetime | None,
    notice: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """触发之后重新读取首页数据，并把提示或错误带回去。"""
    roles = get_role_codes(web.user)
    resolved_now = now or utc_now()
    overview = collect_metrics(
        session,
        now=resolved_now,
        window_days=DEFAULT_WINDOW_DAYS,
        settings=_settings(request),
    )
    reports = queries.list_reports(
        session,
        access_filter=readable_reports_filter(web.user, roles),
        page=1,
        page_size=1,
    )
    newest = reports.items[0] if reports.items else None
    topic_names = _topic_names(session, {newest.topic_id} if newest is not None else set())
    topics = list_topics(session, user=web.user, enabled=True, page_size=TOPIC_MAX_PAGE_SIZE)
    return {
        "current_user": _identity(web.user),
        "overview": overview,
        "newest": newest,
        "newest_topic_name": topic_names.get(newest.topic_id) if newest is not None else None,
        "can_trigger": not roles.isdisjoint(TRIGGER_ROLES),
        "topics": topics.items,
        "collect_limit": _settings(request).pipeline_collect_limit,
        "runtime_mode": _runtime_mode(_settings(request)),
        "active": "home",
        "notice": notice,
        "error": error,
    }


# --------------------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next_url: Annotated[str, Query(alias="next")] = "/ui/home",
) -> HTMLResponse:
    """渲染登录表单。"""
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
    next_url: Annotated[str, Form(alias="next")] = "/ui/home",
    csrf: CsrfField = "",
) -> Response:
    """校验表单里的身份，并把访问令牌存进 `HttpOnly` Cookie。"""
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
    destination = next_url if next_url.startswith("/ui/") else "/ui/home"
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
    """清除会话 Cookie。"""
    verify_csrf(request, csrf)
    response = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# --------------------------------------------------------------------------------------
# 看板
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
    """渲染运维看板：一个观察窗口的指标，加上最新的若干次运行。

    角色校验在任何读取之前执行，所以普通查看者的请求根本不会碰到账本，返回的页面会
    说明原因，而不是给出一张空看板。
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
) -> Response:
    """让看板下旧的运行链接继续可用：把它们指向运行真正的归属页。

    轨迹以前就住在这里，所以从「采集与运行」点开一次运行，感觉像被丢进了「运维看板」：
    URL 写着 `dashboard`，导航高亮的是「运维看板」，唯一的返回路径也通向看板。运行
    属于列出它的那本账，于是页面搬到了 `/runs/{run_id}`；这条路径留作重定向，因为
    链接和书签比一次修复活得久。
    """
    _require_metrics_role(request, session, web)
    return RedirectResponse(
        f"/ui/runs/{run_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# --------------------------------------------------------------------------------------
# 报告
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
    """渲染按权限过滤、分页的报告列表。"""
    # GET 表单会把「全部」提交成 `status=` 这样的空串，而不是缺省参数；查询层用
    # `is not None` 判定，空串会变成 `status == ""`，什么都不匹配。词表外的值同理，
    # 手打错的 URL 不该把只读页面变成空页。两条筛选栏都在这里先归一化。
    resolved_status = status_filter if status_filter in REPORT_STATUSES else None
    resolved_visibility = visibility if visibility in REPORT_VISIBILITIES else None
    page_result = queries.list_reports(
        session,
        access_filter=readable_reports_filter(web.user, get_role_codes(web.user)),
        status=resolved_status,
        visibility=resolved_visibility,
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
            "status_filter": resolved_status or "",
            "visibility_filter": resolved_visibility or "",
            # 直接取自模型自己的元组，而不是在模板里重抄：一个列出状态机并不存在的状态
            # 的筛选项，只会返回空页，看起来像数据有 bug。
            "status_options": REPORT_STATUSES,
            "visibility_options": REPORT_VISIBILITIES,
            # 本页上的发布驱动器是一次写操作：它背后的路由强制 `REPORT_WRITE_ROLES`，
            # 所以按钮也在同一条规则下渲染。过去只读身份会看到按钮、点了却被路由拒绝，
            # 这和侧边栏条目点开就是 403 是同一个毛病。
            "can_write": not get_role_codes(web.user).isdisjoint(REPORT_WRITE_ROLES),
            "active": "reports",
            **_decision_context(session, [int(item.id) for item in page_result.items]),
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
    """渲染每个动作端点都会换回来的报告面板。"""
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
            **_decision_context(session, [int(report.id)]),
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
    """渲染单份报告：引用、结论、复核记录与可执行的动作。"""
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
            **_decision_context(session, [int(report.id)]),
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
    """运行校验器，并把刷新后的面板换回来。"""
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
    """记录一次人工审核决策。"""
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
    """套用发布策略，并在已到期时真正放出这份报告。

    还在撤回窗口内的报告会像 JSON API 那样留在这里排队：只有窗口过去之后才会尝试
    发布，于是 `INV-5` 拦截会以渲染好的 409 呈现，而不是干巴巴一句「已排队」。
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
    """手动跑一次发布驱动器，行为对齐定时扫描。

    范围限于这个用户能读的报告：定时扫描属于系统，人按下的按钮则不是。
    """
    with _audited_action(request, session, web, resource_type="report"):
        verify_csrf(request, csrf)
        _require_write_role(web)
    released = publish_due_reports(
        session,
        access_filter=readable_reports_filter(web.user, get_role_codes(web.user)),
    )
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
    """把报告投递到一个白名单目标，并把刷新后的面板换回来。

    渠道取自目标行而不是表单，所以浏览器无法要求一个该目标并不使用的渠道。重新提交
    同一个目标就是重试路径：服务对已送达的推送做重放，对失败的推送递增尝试次数。
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
    """把历史版本恢复成一个新的草稿版本。"""
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


@router.post("/reports/{report_id}/withdraw", response_class=HTMLResponse)
def withdraw_action(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    report_id: int,
    comment: Annotated[str, Form()] = "",
    csrf: CsrfField = "",
) -> HTMLResponse:
    """把已发布的报告撤回草稿。

    这是离开 `published` 的唯一出路（`published -> draft`），而在此之前界面上根本没有
    这个操作：编辑者能发布一份报告，之后却无从修改 —— 因为编辑要求草稿状态，回滚又
    要求存在第二个可回滚的版本。
    """
    _authorize_action(request, session, web, report_id, csrf)
    try:
        withdraw_report(
            session,
            report_id=report_id,
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
        flash="已撤销发布，报告回到草稿。改完要重新验证、重新发布；撤销不等于删除，版本仍在。",
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
    """应用来自详情页的人工编辑。"""
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
# 推送投递
# --------------------------------------------------------------------------------------


@router.post("/reports/{report_id}/share", response_class=HTMLResponse)
def create_share_action(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    report_id: int,
    csrf: CsrfField = "",
) -> HTMLResponse:
    """生成一条免登录链接，并只渲染一次。

    明文令牌只传给这一次渲染，从不落库。刷新页面会重新提交表单（浏览器会先问一句），
    而不是重放那个字符串，因此「只显示一次」来自 HTTP 语义，而不是前端的记账。
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
    """让一条链接不再可用，且幂等。"""
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
    """渲染投递账本：哪个报告版本去了哪个目的地。

    `list_pushes` 是截断而不是分页，所以页面直接写明上限，而不提供查询根本兑现不了的
    分页控件。
    """
    _require_roles(
        request,
        session,
        web,
        REPORT_WRITE_ROLES,
        resource_type="push",
        message="推送记录仅对管理员和编辑者开放",
    )
    pushes = list_pushes(
        session,
        status=status_filter,
        limit=RECENT_LIMIT,
        # 光有角色还不够：一条投递记录带着报告标题、版本、目的地和错误文本，所以账本
        # 要用报告列表所用的同一套报告 ACL 过滤。否则编辑者能读到一份自己打不开的
        # PRIVATE 报告的元数据。
        access_filter=readable_reports_filter(web.user, get_role_codes(web.user)),
    )
    # 白名单只在那张卡片确实要画出来时才加载。渲染规则和这一条读的是同一个常量，因此
    # 看不到这张卡片的查看者，payload 里也不会带着它的内容 —— 一条把数据留在原地的
    # 展示规则，就是等着下一次调试输出把它漏出去。
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
# 仓库目录
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
    """渲染仓库目录：采集器发现了什么，以及它们归属于哪个主题。"""
    with _audited_action(
        request,
        session,
        web,
        resource_type="topic",
        resource_id=str(topic_id) if topic_id is not None else None,
    ):
        if get_role_codes(web.user).isdisjoint(REPOSITORY_READ_ROLES):
            raise WebForbidden("当前角色不能查看仓库目录")
        # `list_repositories` 写明调用方要在调用*之前*解决主题可见性：目录本身没有
        # 逐行的访问判定，否则收窄到一个调用者无权读取的主题，就成了一种一行一行把它
        # 读出来的办法。
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
            # 筛选项只给出这个身份能读的主题，并受文档化的页大小封顶：再多下去，这个
            # 列表就不再是筛选器，而是第二个目录了。
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
# 采集与研究运行
# --------------------------------------------------------------------------------------


@router.get("/runs", response_class=HTMLResponse)
def runs_page(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    run_type: Annotated[str | None, Query(alias="run_type")] = None,
    run_status: Annotated[str | None, Query(alias="run_status")] = None,
    review: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    """渲染运行账本：流水线阶段、围绕候选池的 Agent 运行，以及回填任务。

    这三者放在同一页，是因为它们正是「采集与运行」的三层：流水线运行是链路中的一个
    阶段（采集 → 快照 → 排名 → 正文缓存，或按主题的 agent/publish/push），Agent 运行
    是 `agent` 阶段内部的研究过程，回填任务则是填充主题候选池的历史回溯。三者都归
    `METRICS_ROLES` —— 它们暴露的是跨主题的成本与失败原因，而不是某一份报告。
    """
    _require_roles(
        request,
        session,
        web,
        METRICS_ROLES,
        resource_type="metrics",
        message="采集与运行仅对管理员和编辑者开放",
    )
    review_filter = review if review in RUN_REVIEW_FILTERS else None
    # 两条筛选栏共用这一页，所以各自把对方的值放在隐藏字段里带过来。「不筛选」于是
    # 会以 `status=` 的形式到达，是空字符串而不是 `None`，而 `AgentRun.status == ""`
    # 什么都不匹配 —— 流水线的筛选会悄悄清空 Agent 卡片，空状态还会把锅甩给数据。
    # 这里用的是与另外两套词汇相同的守卫。
    agent_status_filter = status_filter if status_filter in AGENT_RUN_STATUSES else None
    agent_runs = list_agent_runs(
        session,
        status=agent_status_filter,
        review=review_filter,
        page=page,
        page_size=LIST_PAGE_SIZE,
    )
    # 不在词汇表里的筛选值会被忽略而不是往下传：服务会为它抛错，而 URL 里打错字不该
    # 让一个只读页面变成 500。
    type_filter = run_type if run_type in RUN_TYPES else None
    stage_status_filter = run_status if run_status in RUN_STATUSES else None
    pipeline_runs = list_pipeline_runs(
        session,
        run_type=type_filter,
        status=stage_status_filter,
        limit=PIPELINE_RECENT_LIMIT,
    )
    # 发布/推送阶段的运行记录带着它服务的 report_id，这是从「采集与运行」直达那份报告的
    # 唯一路径。裁决行的 reason_codes 也在这里翻成中文，失败才不是一句只有码的谜语。
    pipeline_report_links: dict[int, dict[str, Any]] = {}
    for run in pipeline_runs:
        metrics = dict(run.metrics_json or {})
        report_id = metrics.get("report_id")
        if not isinstance(report_id, int):
            continue
        reasons = metrics.get("reason_codes") or []
        pipeline_report_links[int(run.id)] = {
            "report_id": report_id,
            "reasons": _publish_reasons(tuple(str(reason) for reason in reasons)),
        }
    return render(
        request,
        "runs.html",
        {
            "current_user": _identity(web.user),
            "page": agent_runs,
            "topic_names": _topic_names(session, {run.topic_id for run in agent_runs.items}),
            "run_reviews": latest_run_reviews(
                session, run_ids=[run.id for run in agent_runs.items]
            ),
            "jobs": summarize_jobs(list_jobs(session, limit=RECENT_LIMIT)),
            "status_filter": agent_status_filter or "",
            "status_options": AGENT_RUN_STATUSES,
            "review_filter": review_filter or "",
            "review_options": RUN_REVIEW_FILTERS,
            "job_statuses": JOB_STATUSES,
            "limit": RECENT_LIMIT,
            "pipeline_runs": pipeline_runs,
            "pipeline_topic_names": _topic_names(
                session, {run.topic_id for run in pipeline_runs if run.topic_id is not None}
            ),
            "run_type_filter": type_filter or "",
            "run_status_filter": stage_status_filter or "",
            "run_types": RUN_TYPES,
            "run_statuses": RUN_STATUSES,
            "pipeline_limit": PIPELINE_RECENT_LIMIT,
            "pipeline_report_links": pipeline_report_links,
            "active": "runs",
        },
    )


#: 被拒绝的复核在页面上用页面自己的话怎么说。服务抛出的是带 `reason` 的英文句子；
#: 界面翻译那个原因，而不是转述整句，于是同一种拒绝无论从哪条路径来，读起来都一样。
RUN_REVIEW_REFUSALS: dict[str, str] = {
    "not_found": "这条运行已经不存在了，可能是被清理掉了。",
    "not_reviewable": (
        "只有「需人工复核」的运行需要记录复核结论。已完成的运行，结论在它产出的那份报告上。"
    ),
    "invalid_verdict": "没有选中有效的复核结论，请重新选择。",
}


def _render_run_detail(
    request: Request,
    session: Session,
    web: WebSession,
    *,
    run_id: int,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """渲染一次运行的轨迹、它的复核历史，以及往其后追加的复评表单。"""
    trace = get_agent_run_trace(session, run_id=run_id)
    if trace is None:
        raise WebNotFound(f"运行 {run_id} 不存在")
    reviews = list_run_reviews(session, run_id=run_id)
    return render(
        request,
        "run_detail.html",
        {
            "current_user": _identity(web.user),
            "trace": trace,
            "unfinished": trace.run.started_at is None or trace.run.finished_at is None,
            "reviews": reviews,
            "latest_review": reviews[-1] if reviews else None,
            # 进入本页的门槛已经是 `METRICS_ROLES`，它与报告写角色是同一个集合，所以
            # 剩下的唯一问题就是这次运行有没有可复核的东西。
            "can_review": trace.run.status == REVIEWABLE_RUN_STATUS,
            "review_verdicts": AGENT_RUN_REVIEW_VERDICTS,
            "error_hints": AGENT_ERROR_HINTS,
            "error": error,
            "notice": notice,
            "active": "runs",
        },
        status_code=status_code,
    )


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail_page(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    run_id: int,
    reviewed: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """渲染一次运行：有序的步骤、每步之下的工具调用，以及它的复核历史。

    「从失败报告追到具体工具调用」这条验收就是在这一页走完的 —— 运维在账本里看到一次
    运行，点开它，落到那个失败的具体工具调用上，并看到它的错误码。

    它挂在 `/runs` 而不是 `/dashboard` 下，因为那才是它所属的列表。旧的看板路径重定向
    到这里：从前从「采集与运行」点开一次运行，导航高亮的是「运维看板」，给出的唯一
    出路也是回看板，读起来就像「这东西跑到别处去了」。
    """
    _require_metrics_role(request, session, web)
    return _render_run_detail(
        request,
        session,
        web,
        run_id=run_id,
        notice="已记录复核结论。" if reviewed == "1" else None,
    )


@router.post("/runs/{run_id}/review", response_class=HTMLResponse)
def run_review_action(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    run_id: int,
    verdict: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
    csrf: CsrfField = "",
) -> Response:
    """给一次停下来等复核的运行追加一条人工结论。

    这个结论不会推动运行：它依旧没有报告，说推动了就等于宣称草稿存在。它做的是让
    下一个读者看见这个案子已经有人看过、以及那个人怎么判的 —— 否则一个
    `needs_review` 行不论有没有人看过都长一个样。
    """
    _require_metrics_role(request, session, web)
    with _audited_action(request, session, web, resource_type="agent_run", resource_id=str(run_id)):
        verify_csrf(request, csrf)
        _require_write_role(web)
    try:
        record_run_review(
            session,
            run_id=run_id,
            verdict=verdict,
            reviewer_id=web.user.id,
            comment=comment.strip() or None,
        )
    except AgentRunReviewError as exc:
        session.rollback()
        return _render_run_detail(
            request,
            session,
            web,
            run_id=run_id,
            error=RUN_REVIEW_REFUSALS.get(exc.reason, "复核结论没有记录成功。"),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(
        f"/ui/runs/{run_id}?reviewed=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# --------------------------------------------------------------------------------------
# 评测
# --------------------------------------------------------------------------------------


@router.get("/evaluations", response_class=HTMLResponse)
def evaluations_page(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    dataset_id: Annotated[int | None, Query(ge=1)] = None,
) -> HTMLResponse:
    """渲染评测界面：已导入的数据集，以及在它们之上跑出的结果。

    刻意只读。跑一次评测要花模型预算，让 `POST /agent/runs` 与
    `POST /topics/{id}/run` 不出现在 API 上的那套理由，同样把触发留给了运维 CLI
    （`python -m open_source_intelligence.evaluation.cli`）。
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
# 审计日志
# --------------------------------------------------------------------------------------


@router.get("/audit", response_class=HTMLResponse)
def audit_page(
    request: Request,
    session: DbSession,
    web: WebSessionDep,
    actor_id: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    resource_type: Annotated[str | None, Query()] = None,
    result: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    """渲染审计账本 —— 每次拒绝、配置变更与发布，最新的在前。"""
    _require_roles(
        request,
        session,
        web,
        AUDIT_READ_ROLES,
        resource_type="audit",
        message="审计日志仅对管理员开放",
    )
    # 输入框留空时，GET 表单提交的是 `actor_id=`（空串）。若把它声明成 `int | None`，
    # pydantic 会在进入函数前就抛 422，页面直接空白。这里按字符串接收，再自己归一化：
    # 空串、非数字、越界值一律视为「不筛选」，合法正整数才转成 int。
    resolved_actor: int | None = None
    if actor_id and actor_id.isdigit() and int(actor_id) >= 1:
        resolved_actor = int(actor_id)
    resolved_result = result if result in AUDIT_RESULTS else None
    page_result = list_audit_logs(
        session,
        actor_id=resolved_actor,
        action=action or None,
        resource_type=resource_type or None,
        result=resolved_result,
        page=page,
        page_size=LIST_PAGE_SIZE,
    )
    return render(
        request,
        "audit.html",
        {
            "current_user": _identity(web.user),
            "page": page_result,
            # 这一列是二进制；API 里有一个函数把它转成常见的点分形式，页面用的也是
            # 同一个，免得不知不觉把它格式化成了 `b'...'`。
            "ip_addresses": {
                int(entry.id): format_ip_address(entry.ip_address) for entry in page_result.items
            },
            "actor_filter": resolved_actor,
            "action_filter": action or "",
            "resource_filter": resource_type or "",
            "result_filter": resolved_result or "",
            "result_options": AUDIT_RESULTS,
            "active": "audit",
        },
    )


# --------------------------------------------------------------------------------------
# 主题配置
# --------------------------------------------------------------------------------------
#
# 这是平台此前唯一缺的那块配置界面。`pipeline/daily.py` 只跑已启用的主题，别的一概
# 不管，所以在没有任何办法新增主题时，整条流水线什么都产不出来，架在它上面的界面
# 不管做得多好，展示的都只是一个空壳。`docs/api-contract.md` §3 把缺失的这套 API
# 记作「设计过但没做」；T19 把它做出来了，这里就是它的界面。
#
# 不放在这个页面上的，和不放在 API 上的是同一件事：触发研究运行。运行要花模型预算，
# 那个触发属于调度器和运维 CLI。

#: 重定向后再取（post-redirect-get）的提示：动作名走在 URL 里，句子在这里查。若让一段
#: 自由文本通过查询参数回显，任何人都能造出一条把话塞进应用嘴里的链接。
TOPIC_NOTICES: dict[str, str] = {
    "created": "已创建主题。",
    "updated": "已保存主题。",
    "deleted": "已删除主题，并解除了它的候选关联。",
    "enabled": "已启用该主题，下一次采集会带上它。",
    "disabled": "已停用该主题，下一次采集不会运行它。",
}

#: 某种拒绝用哪个 HTTP 状态回应。与 JSON API 的划分相同，于是读状态码的客户端在两个
#: 界面上看到的是同一个含义。
TOPIC_ERROR_STATUS: dict[str, int] = {
    "TOPIC_NAME_CONFLICT": status.HTTP_409_CONFLICT,
    "TOPIC_IN_USE": status.HTTP_409_CONFLICT,
    "TOPIC_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "TOPIC_NAME_BLANK": status.HTTP_400_BAD_REQUEST,
}

#: 用页面的语言说出的拒绝。服务层自己的消息是英文，是写给日志行或 API 客户端看的 ——
#: 放进 traceback 里的 `str(exc)` 正合适，但作为运维唯一能看到的那句话就不合适。未
#: 映射的码会落到 `str(exc)`，而不是一句笼统的「操作失败」：新的拒绝应该以它本来的
#: 样子出现，明摆着还没打标签，而不是一块把问题遮住的牌子。
TOPIC_ERROR_MESSAGES: dict[str, str] = {
    "TOPIC_NAME_BLANK": "主题名不能为空。",
    "TOPIC_NAME_CONFLICT": "这个主题名已经被占用，换一个再试。",
    "TOPIC_NOT_FOUND": "主题不存在，可能已经被删掉了。",
    "TOPIC_IN_USE": "这个主题还有历史记录引用它，不能删除",
}


def _topic_refusal(exc: TopicError, *, session: Session, topic_id: int | None) -> str:
    """把领域层的拒绝渲染成页面展示的那句话。

    `TOPIC_IN_USE` 是唯一保留细节的那个，而这些计数来自 `reference_counts`，不是从
    英文消息里解析出来的：「是哪张表在引用它」才是这条拒绝里可操作的那一半，而从句
    子里反读出来，别人一改措辞就会失效。
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
    """一个服务层会拒绝的提交值，在调用之前就被拦下。

    表单把所有东西都当文本发过来，所以数字化字段里填了「八」，得变成一句人能照着改的
    提示，而不是一个夹着 JSON 指针的 422。
    """


def _split_terms(raw: str) -> list[str] | None:
    """把逗号分隔的表单字段变成服务期望的列表。

    空字段意味着「不限」，在库里存为 SQL NULL —— 所以这里返回 `None` 而不是 `[]`。
    空列表意味着「限制到空」，一个仓库都选不出来，与留空字段的本意正好相反。
    """
    terms = [part.strip() for part in raw.split(",") if part.strip()]
    return terms or None


#: 主题表单携带的字段，按显示顺序排列。
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

#: 全新主题的表单从什么值起步。它们就是列默认值，所以不去动它们创建出来的主题，就是
#: 流水线自己会产出的那一行 —— 而不是「新主题」的第二套定义。
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
    """返回主题表单要显示的值，一律转成文本。

    刻意按这个顺序取三个来源。被拒绝之后，运维自己填的内容必须活下来，否则为了改一个
    字段要重打九个；首次打开时，数据库里的那行才是事实；而新建主题没有行，就用上面的
    列默认值顶上。
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
    """把新建/更新表单翻译成服务的字段取值。

    `publish_mode` 在这里校验而不是直接透传：API 路径把它声明成 `Literal`，表单没有，
    放任一个未知取值进到表里，会把一次填错表单变成 `CHECK` 约束冲突，最终以 500 出现，
    而不是带着提示重新渲染的表单。
    """
    publish_mode = (raw.get("publish_mode") or "").strip()
    if publish_mode not in TOPIC_PUBLISH_MODES:
        raise _FormInvalid(f"发布模式必须是 {'、'.join(TOPIC_PUBLISH_MODES)} 之一")
    values: dict[str, Any] = {
        "publish_mode": publish_mode,
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
    """构建主题页的上下文，列表路由与写操作失败时共用。

    写操作失败时重新渲染这个页面，而不是带着消息重定向：句子于是来自我们自己的异常，
    而不是来自写链接的那个人，运维填过的内容也得以保留。
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
    """渲染主题页，并让它的表单拿到一个新的 CSRF 令牌。"""
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
    """为主题写操作把关：先 CSRF，再校验仅管理员可用的角色。

    这里没有报告动作那样的逐行读检查：主题的可见性是按角色划分的，而且只有 `ADMIN`
    能走到这一行 —— 那正是 `can_read_topic` 允许看全部内容的唯一角色。
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
    """渲染主题列表 —— 决定平台采集什么的那份配置。"""
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
    """渲染单个主题的编辑表单。

    它自成页面，而不是把表单折进列表行：一个主题有九个可编辑字段，塞进表格单元格里的
    九字段表单会让表格没法读、表单没法用。列表链到这里，字段就在这个页面上改。
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
    """从配置页新建一个主题。"""
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
        # 在 `create_topic` 上 `publish_mode` 是独立的关键字参数，表里也要求它。把它
        # 留在 `values` 里会把同一个属性设两遍。
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
    """保存单个主题的编辑表单。

    表单带着全部字段，所以这是整体替换而不是打补丁 —— `update_topic` 回传的 `changed`
    也正是这么回事：值真正发生了变化的那些字段。
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
    """不打开编辑表单，直接启用或停用某个主题。"""
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
    """删除一个主题，或者说明还有哪些表在引用它。

    有历史记录的主题删不掉：来自报告、运行和流水线行的外键是 `RESTRICT`，`delete_topic`
    会把它转成一条点名到表的拒绝，而不是让 `IntegrityError` 以 500 冒出来。这条消息
    原样渲染，因为它出自我们自己，而「是哪张表」正是运维要据此行动的那一件事。
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
    """把领域层的拒绝映射到它的 HTTP 状态。"""
    return TOPIC_ERROR_STATUS.get(exc.code, status.HTTP_409_CONFLICT)


def _topics_write_failure(
    request: Request,
    session: Session,
    web: WebSession,
    message: str,
    form: Mapping[str, str],
    status_code: int,
) -> HTMLResponse:
    """带上拒绝原因和运维填过的内容，重新渲染主题列表。"""
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
    """带上拒绝原因和运维填过的内容，重新渲染单个主题的编辑表单。"""
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
