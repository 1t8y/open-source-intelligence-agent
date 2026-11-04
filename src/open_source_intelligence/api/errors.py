"""把每一次 API 失败都渲染成 RFC 7807 Problem Details。

`docs/api-contract.md` §1 要求错误响应是 Problem Details，§9 固定了状态码映射。路由代码只抛
普通的 FastAPI 异常或领域异常；本模块是唯一把它们变成稳定响应体的地方，因此客户端可以只依赖
`code`，而不必去解析英文消息。
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from open_source_intelligence.evaluation.errors import EvaluationError
from open_source_intelligence.notifications.errors import PushError
from open_source_intelligence.observability.errors import ObservabilityError
from open_source_intelligence.reports.errors import (
    PublishPolicyBlocked,
    ReportGenerationError,
    ReportNotFoundError,
    ReportStateError,
)
from open_source_intelligence.reports.sharing import ShareError
from open_source_intelligence.topics.errors import TopicError

PROBLEM_BASE = "https://example.local/problems"

#: 这里直接写出 422：Starlette 为它提供的具名常量已被弃用。
UNPROCESSABLE_CONTENT = 422

#: API 用到的每个 HTTP 状态码所对应的兜底 problem code 与标题。
STATUS_PROBLEMS: dict[int, tuple[str, str]] = {
    status.HTTP_400_BAD_REQUEST: ("VALIDATION_ERROR", "Bad Request"),
    status.HTTP_401_UNAUTHORIZED: ("AUTH_REQUIRED", "Authentication Required"),
    status.HTTP_403_FORBIDDEN: ("FORBIDDEN", "Forbidden"),
    status.HTTP_404_NOT_FOUND: ("NOT_FOUND", "Not Found"),
    status.HTTP_405_METHOD_NOT_ALLOWED: ("METHOD_NOT_ALLOWED", "Method Not Allowed"),
    status.HTTP_409_CONFLICT: ("CONFLICT", "Conflict"),
    UNPROCESSABLE_CONTENT: ("VALIDATION_ERROR", "Validation Error"),
    status.HTTP_429_TOO_MANY_REQUESTS: ("RATE_LIMITED", "Too Many Requests"),
    status.HTTP_500_INTERNAL_SERVER_ERROR: ("INTERNAL_ERROR", "Internal Server Error"),
    status.HTTP_502_BAD_GATEWAY: ("BAD_GATEWAY", "Bad Gateway"),
    status.HTTP_503_SERVICE_UNAVAILABLE: ("SERVICE_UNAVAILABLE", "Service Unavailable"),
}

#: 每个自带稳定 `code` 的领域失败所映射到的 HTTP 状态码。
DOMAIN_PROBLEM_STATUS: dict[str, int] = {
    "NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "REPORT_STATE_INVALID": status.HTTP_409_CONFLICT,
    "PUBLISH_POLICY_BLOCKED": status.HTTP_409_CONFLICT,
    "PUBLISH_ERROR": status.HTTP_409_CONFLICT,
    # 不在允许清单里的目标属于请求有误，而不是权限问题：调用方可能非常清楚这份报告是什么，
    # 只是指错了投递目标。
    "PUSH_TARGET_NOT_ALLOWED": status.HTTP_400_BAD_REQUEST,
    "PUSH_BLOCKED": status.HTTP_409_CONFLICT,
    "PUSH_APPROVAL_REQUIRED": status.HTTP_409_CONFLICT,
    "PUSH_ERROR": status.HTTP_409_CONFLICT,
    # 一次投递尝试都发不出去：要么推送总开关是关的，要么重试预算已经用尽；两者都是上游状态，
    # 而不是调用方犯错。
    "PUSH_DISABLED": status.HTTP_503_SERVICE_UNAVAILABLE,
    "PUSH_DELIVERY_FAILED": status.HTTP_502_BAD_GATEWAY,
    # 评测失败。数据集格式不对或指定了不存在的变体是调用方的错；版本冲突算冲突；子系统关闭算
    # 服务不可用。
    "DATASET_INVALID": status.HTTP_400_BAD_REQUEST,
    # 文件本身格式没问题，只是它绑定的主题在本部署里不存在。这仍然是调用方的错 —— 他们导入
    # 了一份为另一个环境写的数据集 —— 消息里会列出确实存在的主题。
    "DATASET_TOPIC_MISSING": status.HTTP_400_BAD_REQUEST,
    "UNKNOWN_TARGET": status.HTTP_400_BAD_REQUEST,
    "DATASET_VERSION_CONFLICT": status.HTTP_409_CONFLICT,
    "EVALUATION_DISABLED": status.HTTP_503_SERVICE_UNAVAILABLE,
    # 主题配置。名字被另一个主题占用，是调用方换一个名字就能解决的冲突；被历史
    # 记录引用的主题根本不能删，那同样是冲突而不是请求有误 —— 请求本身合法，答案
    # 是「不是这一个」。主题不存在走上面共享的 `NOT_FOUND` 条目。
    "TOPIC_NAME_CONFLICT": status.HTTP_409_CONFLICT,
    "TOPIC_IN_USE": status.HTTP_409_CONFLICT,
    # 空名字是调用方犯错，不是冲突。HTTP 边界上 Pydantic 的
    # `AfterValidator` 会在查这张表之前就以 `422` 拒绝，但同一个 code 仍可能
    # 来自直接调用 `topics.service` 的非 HTTP 调用方；对「你的名字是空的」
    # 回 `409`，会让那个调用方去找一个已经占了该名字的主题。
    "TOPIC_NAME_BLANK": UNPROCESSABLE_CONTENT,
    # 分享链接。报告被拒绝分享算冲突而不是请求有误：调用方说的是真实存在的报告，答案是「不是
    # 这一份」。有效期格式不合法则落到基础 code，那是调用方的错。
    "SHARE_SENSITIVITY_REFUSED": status.HTTP_409_CONFLICT,
    "SHARE_NOT_PUBLISHED": status.HTTP_409_CONFLICT,
    "SHARE_ERROR": status.HTTP_400_BAD_REQUEST,
}


def problem_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    detail: str,
) -> JSONResponse:
    """构造一个 Problem Details 响应，并附上请求关联 ID。"""
    default_code, default_title = STATUS_PROBLEMS.get(
        status_code,
        ("ERROR", "Error"),
    )
    resolved_code = code or default_code
    return JSONResponse(
        status_code=status_code,
        content={
            "type": f"{PROBLEM_BASE}/{resolved_code.lower().replace('_', '-')}",
            "title": default_title,
            "status": status_code,
            "detail": detail,
            "instance": request.url.path,
            "code": resolved_code,
            "request_id": str(getattr(request.state, "request_id", "unknown")),
        },
    )


def _domain_status(code: str, fallback: int) -> int:
    """返回领域错误码映射到的 HTTP 状态码。"""
    return DOMAIN_PROBLEM_STATUS.get(code, fallback)


def add_problem_handlers(app: FastAPI) -> None:
    """把 Problem Details 处理器注册到应用上。

    `create_app` 在装配阶段调用一次；此后每个请求只要抛出下面对应的异常，
    Starlette 就在该请求的上下文里调用匹配的处理器，把它渲染成 Problem
    Details，而不是交给默认错误页。
    """

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        """把 FastAPI/Starlette 的 HTTP 错误渲染成 Problem Details。

        处理器优先用异常自己带的 `code`（领域层抛出时挂上的属性），没有才交给状态码表的兜底
        值，所以同一个状态码可以承载不同含义。
        """
        explicit = getattr(exc, "code", None)
        code = explicit if isinstance(explicit, str) else ""
        return problem_response(
            request,
            status_code=exc.status_code,
            code=code,
            detail=str(exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        """概括字段级校验错误，且不回显整份请求体。

        每一项都把字段路径和原因配成一对：只有路径，调用方知道去哪儿看，
        却不知道要改什么 —— 对「时间戳必须带时区偏移」这类规则来说，
        原因本身就是全部答案。原因取自 Pydantic 的消息模板而不是出错的输入值，
        因此写进响应也不算回显请求体。
        """
        details: list[str] = []
        for error in exc.errors()[:5]:
            location = ".".join(str(part) for part in error.get("loc", ()))
            reason = str(error.get("msg", "invalid value"))
            if reason.startswith("Value error, "):
                reason = reason[len("Value error, ") :]
            details.append(f"{location}: {reason}" if location else reason)
        summarized = "; ".join(details) if details else "invalid request"
        return problem_response(
            request,
            status_code=UNPROCESSABLE_CONTENT,
            code="VALIDATION_ERROR",
            detail=f"invalid request fields: {summarized}" if details else summarized,
        )

    @app.exception_handler(ReportNotFoundError)
    async def report_not_found_handler(
        request: Request,
        exc: ReportNotFoundError,
    ) -> JSONResponse:
        """把报告或报告版本不存在渲染成 `404 NOT_FOUND`。"""
        return problem_response(
            request,
            status_code=status.HTTP_404_NOT_FOUND,
            code=exc.code,
            detail=str(exc),
        )

    @app.exception_handler(ReportStateError)
    async def report_state_handler(
        request: Request,
        exc: ReportStateError,
    ) -> JSONResponse:
        """把非法的状态机迁移渲染成 `409 REPORT_STATE_INVALID`。"""
        return problem_response(
            request,
            status_code=_domain_status(exc.code, status.HTTP_409_CONFLICT),
            code=exc.code,
            detail=str(exc),
        )

    @app.exception_handler(PublishPolicyBlocked)
    async def publish_blocked_handler(
        request: Request,
        exc: PublishPolicyBlocked,
    ) -> JSONResponse:
        """把被拦截的发布渲染成 `409 PUBLISH_POLICY_BLOCKED`。"""
        return problem_response(
            request,
            status_code=_domain_status(exc.code, status.HTTP_409_CONFLICT),
            code=exc.code,
            detail=str(exc),
        )

    @app.exception_handler(ReportGenerationError)
    async def report_generation_handler(
        request: Request,
        exc: ReportGenerationError,
    ) -> JSONResponse:
        """把被拒绝的撰写/编辑请求渲染成 `400 VALIDATION_ERROR`。"""
        return problem_response(
            request,
            status_code=status.HTTP_400_BAD_REQUEST,
            code="VALIDATION_ERROR",
            detail=str(exc),
        )

    @app.exception_handler(ShareError)
    async def share_error_handler(
        request: Request,
        exc: ShareError,
    ) -> JSONResponse:
        """用各自稳定的 code 渲染被拒绝的分享链接操作。

        注册基类即可覆盖全部三种失败；其中最要紧的是敏感度拒绝，因为正是
        这道守卫拦住了「免登录地址打开到内部内容」的路径。
        """
        return problem_response(
            request,
            status_code=_domain_status(exc.code, status.HTTP_409_CONFLICT),
            code=exc.code,
            detail=str(exc),
        )

    @app.exception_handler(PushError)
    async def push_error_handler(
        request: Request,
        exc: PushError,
    ) -> JSONResponse:
        """用各自稳定的推送 code 渲染被拒绝或无法完成的推送。

        注册基类即可覆盖整条继承链，因此新增一种推送失败只需声明一个 `code` 并登记映射，就能
        自动成为正确的 Problem Details 响应。
        """
        return problem_response(
            request,
            status_code=_domain_status(exc.code, status.HTTP_409_CONFLICT),
            code=exc.code,
            detail=str(exc),
        )

    @app.exception_handler(EvaluationError)
    async def evaluation_error_handler(
        request: Request,
        exc: EvaluationError,
    ) -> JSONResponse:
        """用各自稳定的 code 渲染评测失败。

        基类处理器覆盖整条评测继承链：`NOT_FOUND` 落到 `404`，调用方犯错的 code 落到 `400`，
        版本冲突落到 `409`，子系统关闭落到 `503` —— 全部出自上面那一张映射表。
        """
        return problem_response(
            request,
            status_code=_domain_status(exc.code, status.HTTP_409_CONFLICT),
            code=exc.code,
            detail=str(exc),
        )

    @app.exception_handler(ObservabilityError)
    async def observability_error_handler(
        request: Request,
        exc: ObservabilityError,
    ) -> JSONResponse:
        """用各自稳定的 code 渲染概览或运行台账的失败。

        基类处理器覆盖整条继承链：请求一次并不存在的运行就对应上面映射表里的 `NOT_FOUND`
        条目，因此会落到 `404`，不需要任何特例分支。
        """
        return problem_response(
            request,
            status_code=_domain_status(exc.code, status.HTTP_409_CONFLICT),
            code=exc.code,
            detail=str(exc),
        )

    @app.exception_handler(TopicError)
    async def topic_error_handler(
        request: Request,
        exc: TopicError,
    ) -> JSONResponse:
        """用各自稳定的 code 渲染被拒绝的主题变更。

        基类处理器覆盖整条继承链：不存在的主题经共享的 `NOT_FOUND` 条目落到
        `404`，另外两种冲突落到 `409`，无需为每个子类各写一条分支来保持同步。
        """
        return problem_response(
            request,
            status_code=_domain_status(exc.code, status.HTTP_409_CONFLICT),
            code=exc.code,
            detail=str(exc),
        )
