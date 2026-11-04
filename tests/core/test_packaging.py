"""守护 wheel 与容器打包边界的回归检查。"""

from pathlib import Path


def test_web_templates_and_static_assets_are_declared_as_package_data() -> None:
    """证明用 wheel 安装后仍会带上服务端渲染界面所需的静态资源。"""
    project_root = Path(__file__).resolve().parents[2]
    pyproject = (project_root / "pyproject.toml").read_text(encoding="utf-8")

    assert "web/templates/*.html" in pyproject
    assert "web/templates/partials/*.html" in pyproject
    assert "web/static/*" in pyproject


def test_container_image_copies_the_shipped_evaluation_datasets() -> None:
    """证明应用镜像里存在 T14 数据集的那条默认路径。"""
    project_root = Path(__file__).resolve().parents[2]
    dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY datasets ./datasets" in dockerfile


def test_ci_installs_the_exact_toolchain_and_runs_clean_mypy() -> None:
    """证明 CI 显式安装锁定版本的工具链，并绕开增量缓存。"""
    project_root = Path(__file__).resolve().parents[2]
    workflow = (project_root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    lock = (project_root / "requirements-ci.txt").read_text(encoding="utf-8")
    runtime = (project_root / "requirements.txt").read_text(encoding="utf-8")

    assert "python -m pip install --no-cache-dir -r requirements-ci.txt" in workflow
    assert "python -m pytest -p no:cacheprovider" in workflow
    assert "python -m mypy --no-incremental src" in workflow
    assert workflow.count("runs-on: ubuntu-24.04") == 2
    assert "runs-on: ubuntu-latest" not in workflow
    # 运行时的版本锁定写在 `requirements.txt` 里，`requirements-ci.txt`
    # 通过 `-r` 把它拉进来；
    # 仅在测试时用到的工具留在 `requirements-ci.txt`，镜像因此永远不会带上它们。
    assert "-r requirements.txt" in lock
    assert "mypy==1.11.2" in lock
    assert "pytest==7.4.4" in lock
    assert "sqlalchemy==2.0.34" in runtime
    assert "pydantic==2.10.6" in runtime
    # 运行时镜像必须装同一套锁定版本（经由 requirements.txt），
    # 而不是 CI 的那份锁文件。
    dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")
    assert "-r requirements.txt" in dockerfile
    assert "--no-deps" in dockerfile
