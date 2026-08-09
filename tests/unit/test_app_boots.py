"""Smoke test ensuring the FastAPI application boots and is importable."""


def test_app_imports_and_has_title() -> None:
    from app.main import app

    assert app.title
    assert isinstance(app.title, str)
