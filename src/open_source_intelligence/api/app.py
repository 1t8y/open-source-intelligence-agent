"""FastAPI application factory."""

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from open_source_intelligence import __version__
from open_source_intelligence.core.config import Settings, get_settings
from open_source_intelligence.core.logging import configure_logging

from .routes.health import router as health_router


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create an application with explicit settings injection."""
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)

    app = FastAPI(
        title=resolved_settings.app_name,
        version=__version__,
        docs_url="/docs" if resolved_settings.docs_enabled else None,
        redoc_url="/redoc" if resolved_settings.docs_enabled else None,
    )
    app.state.settings = resolved_settings
    app.include_router(health_router)

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        """Open interactive docs for browser previews."""
        destination = "/docs" if resolved_settings.docs_enabled else "/health"
        return RedirectResponse(url=destination, status_code=307)

    return app
