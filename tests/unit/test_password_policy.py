"""Unit tests for the reusable password-policy validator.

Business decision (confirmed): minimum 12 characters, no forced
complexity rules (no required uppercase/digit/symbol classes).
"""

import pytest
from pydantic import BaseModel

from app.schemas.auth import PasswordStr


class _Holder(BaseModel):
    password: PasswordStr


def test_password_shorter_than_twelve_chars_is_rejected() -> None:
    with pytest.raises(ValueError):
        _Holder(password="a" * 11)


def test_password_of_exactly_twelve_chars_is_accepted() -> None:
    holder = _Holder(password="a" * 12)
    assert holder.password == "a" * 12


def test_password_with_no_complexity_requirements_is_accepted() -> None:
    # Purely lowercase, no digits/symbols/uppercase — must still pass.
    holder = _Holder(password="lowercaseonly")
    assert holder.password == "lowercaseonly"
