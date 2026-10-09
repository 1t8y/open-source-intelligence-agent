"""Tests for the FastAPI health endpoint."""

from fastapi.testclient import TestClient

from open_source_intelligence import __version__
from open_source_intelligence.api.app import create_app
from open_source_intelligence.core.config import Settings


def build_settings() -> Settings:
    """Create deterministic test settings without reading a local .env file."""
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+pymysql://app:test@localhost:3306/intelligence",
        redis_url="redis://localhost:6379/15",
        secret_key="x" * 32,
        log_format="json",
    )


def test_health_returns_service_contract() -> None:
    """The liveness endpoint should return service identity and version."""
    with TestClient(create_app(build_settings())) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "open-source-intelligence-agent",
        "version": __version__,
    }


def test_docs_can_be_disabled() -> None:
    """Disabled docs must not expose Swagger or ReDoc routes."""
    settings = build_settings().model_copy(update={"docs_enabled": False})
    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404


def test_root_redirects_to_docs_for_browser_preview() -> None:
    """The root URL should open the interactive documentation."""
    with TestClient(create_app(build_settings())) as client:
        response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/docs"


def test_root_redirects_to_health_when_docs_are_disabled() -> None:
    """A docs-disabled deployment should still expose a browser-safe root."""
    settings = build_settings().model_copy(update={"docs_enabled": False})
    with TestClient(create_app(settings)) as client:
        response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/health"
