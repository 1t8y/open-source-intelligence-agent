"""组装 FastAPI 应用实例。

本模块把配置、日志、数据库引擎、请求关联中间件、Problem Details 处理器与全部路由按
固定顺序接好，调用方只需拿到返回值，不必关心装配顺序。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from open_source_intelligence import __version__
from open_source_intelligence.core.config import Settings, get_settings
from open_source_intelligence.core.logging import configure_logging
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.web.router import add_web_surface
from open_source_intelligence.web.router import router as web_router
from open_source_intelligence.web.sharing import router as share_router

from .errors import add_problem_handlers
from .middleware import add_request_id_middleware
from .routes.agent_runs import router as agent_runs_router
from .routes.audit import router as audit_router
from .routes.auth import router as auth_router
from .routes.evaluations import router as evaluations_router
from .routes.health import router as health_router
from .routes.metrics import router as metrics_router
from .routes.push import router as push_router
from .routes.reports import router as reports_router
from .routes.repositories import router as repositories_router
from .routes.topics import router as topics_router


def create_app(settings: Settings | None = None) -> FastAPI:
    """按显式注入的配置创建应用实例。

    生产环境由 `uvicorn` 的 `--factory` 模式在进程启动时调用，目标模块路径是
    `open_source_intelligence.api.app:create_app`；测试夹具与
    `scripts/check_docs_drift.py` 也直接调用它。不传 `settings` 时回落到进程内唯一的
    `get_settings()`，显式传入则是为了让不读 `.env` 的调用方（干净 checkout 的
    CI）也能装配应用。
    """
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)
    engine = create_database_engine(resolved_settings.database_url)
    session_factory = create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(
        title=resolved_settings.app_name,
        version=__version__,
        docs_url="/docs" if resolved_settings.docs_enabled else None,
        redoc_url="/redoc" if resolved_settings.docs_enabled else None,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    add_request_id_middleware(app)
    add_problem_handlers(app)
    add_web_surface(app)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(reports_router)
    app.include_router(push_router)
    app.include_router(evaluations_router)
    app.include_router(metrics_router)
    app.include_router(agent_runs_router)
    app.include_router(repositories_router)
    app.include_router(topics_router)
    app.include_router(audit_router)
    app.include_router(share_router)
    app.include_router(web_router)

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        """为浏览器预览打开交互式文档。"""
        destination = "/docs" if resolved_settings.docs_enabled else "/health"
        return RedirectResponse(url=destination, status_code=307)

    return app
