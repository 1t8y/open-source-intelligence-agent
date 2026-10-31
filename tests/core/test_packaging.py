"""Regression checks for wheel and container packaging boundaries."""

from pathlib import Path


def test_web_templates_and_static_assets_are_declared_as_package_data() -> None:
    """A wheel install must retain the server-rendered UI assets."""
    project_root = Path(__file__).resolve().parents[2]
    pyproject = (project_root / "pyproject.toml").read_text(encoding="utf-8")

    assert "web/templates/*.html" in pyproject
    assert "web/templates/partials/*.html" in pyproject
    assert "web/static/*" in pyproject


def test_container_image_copies_the_shipped_evaluation_datasets() -> None:
    """The default T14 dataset path must exist inside the application image."""
    project_root = Path(__file__).resolve().parents[2]
    dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY datasets ./datasets" in dockerfile


def test_ci_installs_the_exact_toolchain_and_runs_clean_mypy() -> None:
    """CI must resolve the pinned tools explicitly and bypass incremental caches."""
    project_root = Path(__file__).resolve().parents[2]
    workflow = (project_root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    lock = (project_root / "requirements-ci.txt").read_text(encoding="utf-8")

    assert "python -m pip install --no-cache-dir -r requirements-ci.txt" in workflow
    assert "python -m pytest -p no:cacheprovider" in workflow
    assert "python -m mypy --no-incremental src" in workflow
    assert "mypy==1.11.2" in lock
    assert "pytest==7.4.4" in lock
    assert "sqlalchemy==2.0.34" in lock
    assert "pydantic==2.10.6" in lock
