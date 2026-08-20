"""Unit test for the `SecretEncryptionUnavailableError` -> HTTP 503
exception handler registered on the real `app.main.app` instance (stage 5
— multichannel).

Same convention as `tests/unit/test_not_found_exception_handler.py`: mount
a tiny ad-hoc route only for the duration of the test, to exercise real
FastAPI exception-handling dispatch rather than calling the handler
function directly.
"""

from fastapi.testclient import TestClient

from app.core.crypto import SecretEncryptionUnavailableError
from app.main import app


def test_secret_encryption_unavailable_error_maps_to_503() -> None:
    @app.get("/__test_only_secret_encryption_unavailable_route__")
    def _raise_secret_encryption_unavailable() -> None:
        raise SecretEncryptionUnavailableError("no key configured")

    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/__test_only_secret_encryption_unavailable_route__")
    finally:
        app.router.routes = [
            route
            for route in app.router.routes
            if getattr(route, "path", None)
            != "/__test_only_secret_encryption_unavailable_route__"
        ]

    assert response.status_code == 503
