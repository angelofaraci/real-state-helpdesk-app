"""Unit tests for `app.schemas.user`."""

import pytest
from pydantic import ValidationError

from app.schemas.user import InviteUserRequest


@pytest.mark.parametrize("role", ["tenant", "owner", "agent", "admin"])
def test_invite_user_request_accepts_every_known_role(role: str) -> None:
    req = InviteUserRequest(name="Jane Doe", email="jane@example.com", role=role)
    assert req.role == role


def test_invite_user_request_rejects_an_unknown_role() -> None:
    with pytest.raises(ValidationError):
        InviteUserRequest(name="Jane Doe", email="jane@example.com", role="superuser")


def test_invite_user_request_rejects_an_invalid_email() -> None:
    with pytest.raises(ValidationError):
        InviteUserRequest(name="Jane Doe", email="not-an-email", role="agent")
