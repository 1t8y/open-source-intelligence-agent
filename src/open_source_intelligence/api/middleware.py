"""HTTP request correlation middleware."""

from uuid import uuid4

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import RequestResponseEndpoint


def add_request_id_middleware(app: FastAPI) -> None:
    """Attach a bounded request ID to every request and response."""

    @app.middleware("http")
    async def request_id_middleware(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        incoming = request.headers.get("X-Request-ID", "").strip()
        correlation_id = incoming[:128] if incoming else uuid4().hex
        request.state.request_id = correlation_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = correlation_id
        return response
