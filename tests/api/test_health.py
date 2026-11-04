"""守护健康检查端点：探活响应、文档开关与根路径跳转都按对外契约行事。"""

from fastapi.testclient import TestClient

from open_source_intelligence import __version__
from open_source_intelligence.api.app import create_app
from open_source_intelligence.core.config import Settings


def build_settings() -> Settings:
    """构造确定性的测试配置，不去读本地 `.env` 文件。"""
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+pymysql://app:test@localhost:3306/intelligence",
        redis_url="redis://localhost:6379/15",
        secret_key="x" * 32,
        log_format="json",
    )


def test_health_returns_service_contract() -> None:
    """证明探活端点给出的是服务身份与版本，而不是一个空的状态码。"""
    with TestClient(create_app(build_settings())) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "open-source-intelligence-agent",
        "version": __version__,
    }


def test_docs_can_be_disabled() -> None:
    """证明关掉文档开关后，`/docs` 与 `/redoc` 两条路由都必须消失。

    只要其中一条还活着，就等于在生产环境里留了一个不需要鉴权的接口清单。
    """
    settings = build_settings().model_copy(update={"docs_enabled": False})
    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404


def test_root_redirects_to_docs_for_browser_preview() -> None:
    """证明浏览器打开根路径时，会被带到可交互的文档页。"""
    with TestClient(create_app(build_settings())) as client:
        response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/docs"


def test_root_redirects_to_health_when_docs_are_disabled() -> None:
    """证明关掉文档的部署里，根路径仍会落到一个浏览器能安全访问的地址。

    此时若还指向 `/docs`，访客会撞上一个 404 —— 根路径本身就是部署的对外门面。
    """
    settings = build_settings().model_copy(update={"docs_enabled": False})
    with TestClient(create_app(settings)) as client:
        response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/health"
