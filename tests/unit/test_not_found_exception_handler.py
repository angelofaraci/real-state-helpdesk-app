"""Unit tests for the single `NotFoundError` -> HTTP 404 exception handler
registered on the real `app.main.app` instance.

Uses `TestClient` against a tiny ad-hoc route mounted only for the duration
of the test (never added to the real API surface), which exercises the
actual FastAPI exception-handling dispatch (status code + JSON body +
message redaction) rather than calling the handler function directly.
"""

from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.exceptions import NotFoundError
from app.main import app


def test_not_found_error_maps_to_404_with_generic_body() -> None:
    missing_id = uuid4()

    @app.get("/__test_only_not_found_route__")
    def _raise_not_found() -> None:
        raise NotFoundError("Widget", missing_id)

    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/__test_only_not_found_route__")
    finally:
        app.router.routes = [
            route
            for route in app.router.routes
            if getattr(route, "path", None) != "/__test_only_not_found_route__"
        ]

    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}
    # The internal exception message (with the row's name/id) must never
    # leak into the response body.
    assert str(missing_id) not in response.text
    assert "Widget" not in response.text
