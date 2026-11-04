"""为每个 HTTP 请求建立关联 ID 的中间件。

一个请求进来只做两件事：给 `request.state.request_id` 赋值，供日志与 Problem Details 引用；
把这个值原样写回响应的 `X-Request-ID` 头，让调用方能把一次失败和一段服务端日志对上。
"""

from uuid import uuid4

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import RequestResponseEndpoint


def add_request_id_middleware(app: FastAPI) -> None:
    """给每个请求与响应附上长度受限的请求 ID。

    调用方自带的 `X-Request-ID` 截断到 128 字符后沿用，缺失时生成一个 uuid4；截断是有意的，
    头部长度不该由外部输入决定。
    """

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
