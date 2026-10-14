"""FastAPI application factory."""

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

from .errors import add_problem_handlers
from .middleware import add_request_id_middleware
from .routes.agent_runs import router as agent_runs_router
from .routes.auth import router as auth_router
from .routes.evaluations import router as evaluations_router
from .routes.health import router as health_router
from .routes.metrics import router as metrics_router
from .routes.push import router as push_router
from .routes.reports import router as reports_router


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create an application with explicit settings injection."""
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
    app.include_router(web_router)

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        """Open interactive docs for browser previews."""
        destination = "/docs" if resolved_settings.docs_enabled else "/health"
        return RedirectResponse(url=destination, status_code=307)

    return app
