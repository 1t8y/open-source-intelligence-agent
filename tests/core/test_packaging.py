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
