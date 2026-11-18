"""守护异步边界：事件循环线程上不得直接跑同步阻塞工作。

同步页面端点由 Starlette 自动放进线程池，但 `@app.exception_handler` 注册的协程
处理器与 lifespan 会被直接 await —— 它们内部的同步渲染和连接池关闭会占住事件循环。
这里不测响应内容（那是别的用例的职责），只测「同步工作发生在哪个执行上下文」。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import Response
from support import build_settings

from open_source_intelligence.api.app import create_app
from open_source_intelligence.web.router import add_web_surface, templates
from open_source_intelligence.web.security import WebForbidden, WebNotFound


def _on_event_loop() -> bool:
    """返回当前线程是否正在运行事件循环。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def test_lifespan_disposes_the_engine_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关闭连接池是阻塞 IO，不能由 lifespan 在事件循环线程里直接做。"""
    app = create_app(settings=build_settings())
    engine = app.state.engine
    seen: list[bool] = []

    def spy(*_args: Any, **_kwargs: Any) -> None:
        seen.append(_on_event_loop())

    monkeypatch.setattr(engine, "dispose", spy)

    async def drive() -> None:
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(drive())

    assert seen == [False]


def test_web_error_pages_are_rendered_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 与 404 页面是同步 Jinja 渲染，三条分支都必须让出事件循环。"""
    app = FastAPI()
    add_web_surface(app)

    @app.get("/boom")
    def boom() -> None:
        raise WebForbidden("denied")

    @app.get("/missing")
    def missing() -> None:
        raise WebNotFound("missing")

    seen: list[bool] = []

    def spy(
        _request: Any,
        _name: str,
        _context: dict[str, Any],
        **kwargs: Any,
    ) -> Response:
        seen.append(_on_event_loop())
        return Response("denied", status_code=kwargs.get("status_code", 403))

    monkeypatch.setattr(templates, "TemplateResponse", spy)
    client = TestClient(app)

    assert client.get("/boom").status_code == 403
    assert client.get("/boom", headers={"HX-Request": "true"}).status_code == 403
    assert client.get("/missing").status_code == 404

    assert seen == [False, False, False]
