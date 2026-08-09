"""Unit tests for `app.schemas.auth` request/response shapes."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.auth import AcceptInviteRequest, LoginRequest, MeResponse, TokenPairResponse


def test_login_request_requires_email_and_password() -> None:
    request = LoginRequest(email="user@example.com", password="whatever-password")
    assert request.email == "user@example.com"
    assert request.password == "whatever-password"


def test_login_request_rejects_invalid_email() -> None:
    with pytest.raises(ValidationError):
        LoginRequest(email="not-an-email", password="whatever-password")


def test_token_pair_response_shape() -> None:
    response = TokenPairResponse(
        access_token="a.b.c", refresh_token="opaque-token", token_type="bearer"
    )
    assert response.access_token == "a.b.c"
    assert response.refresh_token == "opaque-token"
    assert response.token_type == "bearer"


def test_token_pair_response_defaults_token_type_to_bearer() -> None:
    response = TokenPairResponse(access_token="a.b.c", refresh_token="opaque-token")
    assert response.token_type == "bearer"


def test_me_response_shape() -> None:
    user_id = uuid4()
    org_id = uuid4()
    response = MeResponse(
        id=user_id,
        organization_id=org_id,
        name="Jane Doe",
        email="jane@example.com",
        role="agent",
        status="active",
    )
    assert response.id == user_id
    assert response.organization_id == org_id
    assert response.role == "agent"


def test_accept_invite_request_accepts_a_token_and_valid_new_password() -> None:
    request = AcceptInviteRequest(token="raw-invite-token", new_password="a" * 12)
    assert request.token == "raw-invite-token"
    assert request.new_password == "a" * 12


def test_accept_invite_request_rejects_a_password_below_minimum_length() -> None:
    with pytest.raises(ValidationError):
        AcceptInviteRequest(token="raw-invite-token", new_password="short")
