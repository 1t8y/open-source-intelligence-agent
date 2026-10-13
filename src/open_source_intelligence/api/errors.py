"""RFC 7807 Problem Details for every API failure.

`docs/api-contract.md` §1 requires error responses to be Problem Details and §9 fixes the
status-code mapping. Router code raises ordinary FastAPI or domain exceptions; this module is
the single place that turns them into a stable body, so a client can rely on `code` without
parsing English messages.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from open_source_intelligence.notifications.errors import PushError
from open_source_intelligence.reports.errors import (
    PublishPolicyBlocked,
    ReportGenerationError,
    ReportNotFoundError,
    ReportStateError,
)

PROBLEM_BASE = "https://example.local/problems"

#: 422 is spelled out because the named Starlette constant for it is deprecated.
UNPROCESSABLE_CONTENT = 422

#: Fallback problem code and title for every HTTP status the API uses.
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

#: Problem code for each domain failure that carries its own stable `code`.
DOMAIN_PROBLEM_STATUS: dict[str, int] = {
    "NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "REPORT_STATE_INVALID": status.HTTP_409_CONFLICT,
    "PUBLISH_POLICY_BLOCKED": status.HTTP_409_CONFLICT,
    "PUBLISH_ERROR": status.HTTP_409_CONFLICT,
    # A destination that is not on the allowlist is a bad request, not a permission problem:
    # the caller may know the report perfectly well and simply asked for the wrong target.
    "PUSH_TARGET_NOT_ALLOWED": status.HTTP_400_BAD_REQUEST,
    "PUSH_BLOCKED": status.HTTP_409_CONFLICT,
    "PUSH_APPROVAL_REQUIRED": status.HTTP_409_CONFLICT,
    "PUSH_ERROR": status.HTTP_409_CONFLICT,
    # No delivery attempt could be made, either because pushing is switched off or because the
    # retry budget is spent; both are upstream conditions rather than caller mistakes.
    "PUSH_DISABLED": status.HTTP_503_SERVICE_UNAVAILABLE,
    "PUSH_DELIVERY_FAILED": status.HTTP_502_BAD_GATEWAY,
}


def problem_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    detail: str,
) -> JSONResponse:
    """Build one Problem Details response with the request correlation ID attached."""
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
    """Return the HTTP status a domain error code maps to."""
    return DOMAIN_PROBLEM_STATUS.get(code, fallback)


def add_problem_handlers(app: FastAPI) -> None:
    """Register the Problem Details handlers on the application."""

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        """Render FastAPI/Starlette HTTP errors as Problem Details."""
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
        """Summarize field-level validation errors without echoing the whole payload."""
        fields = ", ".join(
            ".".join(str(part) for part in error.get("loc", ())) for error in exc.errors()[:5]
        )
        return problem_response(
            request,
            status_code=UNPROCESSABLE_CONTENT,
            code="VALIDATION_ERROR",
            detail=f"invalid request fields: {fields}" if fields else "invalid request",
        )

    @app.exception_handler(ReportNotFoundError)
    async def report_not_found_handler(
        request: Request,
        exc: ReportNotFoundError,
    ) -> JSONResponse:
        """Render a missing report or report version as `404 NOT_FOUND`."""
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
        """Render an illegal state-machine move as `409 REPORT_STATE_INVALID`."""
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
        """Render a blocked release as `409 PUBLISH_POLICY_BLOCKED`."""
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
        """Render a rejected compose/edit request as `400 VALIDATION_ERROR`."""
        return problem_response(
            request,
            status_code=status.HTTP_400_BAD_REQUEST,
            code="VALIDATION_ERROR",
            detail=str(exc),
        )

    @app.exception_handler(PushError)
    async def push_error_handler(
        request: Request,
        exc: PushError,
    ) -> JSONResponse:
        """Render a refused or impossible push with its own stable push code.

        Registering the base class covers the whole hierarchy, so a new push failure only has to
        declare a `code` and a mapping to become a correct Problem Details response.
        """
        return problem_response(
            request,
            status_code=_domain_status(exc.code, status.HTTP_409_CONFLICT),
            code=exc.code,
            detail=str(exc),
        )
