"""Token verification is a trust boundary: every rejection path gets a test."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import HTTPException, Request

from app.auth import Caller, get_caller
from app.config import Settings

SECRET = "test-secret-not-used-anywhere-real"
ACCOUNT_ID = "6f1c2f2e-0b5a-4d6e-9f3a-2b7c8d9e0f11"


def settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",  # type: ignore[arg-type]
        jwt_secret=SECRET,  # type: ignore[arg-type]
    )


def make_token(**overrides: object) -> str:
    payload: dict[str, object] = {
        "sub": ACCOUNT_ID,
        "username": "rithik",
        "role": "ADMIN",
        "companyId": "c0ffee00-0000-4000-8000-000000000001",
        "type": "access",
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
    }
    payload.update(overrides)
    return jwt.encode(payload, SECRET, algorithm="HS256")


def request_with(headers: dict[str, str] | None = None, cookies: str | None = None) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookies:
        raw_headers.append((b"cookie", cookies.encode()))
    return Request({"type": "http", "headers": raw_headers, "method": "GET", "path": "/"})


def test_accepts_bearer_token() -> None:
    caller = get_caller(request_with({"Authorization": f"Bearer {make_token()}"}), settings())
    assert caller == Caller(
        sub=ACCOUNT_ID, username="rithik", role="ADMIN", companyId="c0ffee00-0000-4000-8000-000000000001"
    )


def test_accepts_cookie_token() -> None:
    caller = get_caller(request_with(cookies=f"access_token={make_token()}"), settings())
    assert caller.account_id == ACCOUNT_ID


def test_rejects_missing_token() -> None:
    with pytest.raises(HTTPException) as exc:
        get_caller(request_with(), settings())
    assert exc.value.status_code == 401


def test_rejects_refresh_token() -> None:
    """cableCORE refuses a refresh token on protected routes; aihub must too."""
    token = make_token(type="refresh")
    with pytest.raises(HTTPException) as exc:
        get_caller(request_with({"Authorization": f"Bearer {token}"}), settings())
    assert exc.value.status_code == 401


def test_rejects_expired_token() -> None:
    token = make_token(exp=int((datetime.now(UTC) - timedelta(seconds=1)).timestamp()))
    with pytest.raises(HTTPException) as exc:
        get_caller(request_with({"Authorization": f"Bearer {token}"}), settings())
    assert exc.value.status_code == 401


def test_rejects_token_signed_with_another_secret() -> None:
    token = jwt.encode(
        {"sub": ACCOUNT_ID, "type": "access", "exp": 9999999999}, "wrong-secret", algorithm="HS256"
    )
    with pytest.raises(HTTPException) as exc:
        get_caller(request_with({"Authorization": f"Bearer {token}"}), settings())
    assert exc.value.status_code == 401


def test_rejects_unsigned_token() -> None:
    """An alg=none token must never be accepted."""
    token = jwt.encode({"sub": ACCOUNT_ID, "type": "access", "exp": 9999999999}, key="", algorithm="none")
    with pytest.raises(HTTPException) as exc:
        get_caller(request_with({"Authorization": f"Bearer {token}"}), settings())
    assert exc.value.status_code == 401


def test_rejects_token_without_subject() -> None:
    token = jwt.encode({"type": "access", "exp": 9999999999}, SECRET, algorithm="HS256")
    with pytest.raises(HTTPException) as exc:
        get_caller(request_with({"Authorization": f"Bearer {token}"}), settings())
    assert exc.value.status_code == 401


def test_rejects_token_without_expiry() -> None:
    token = jwt.encode({"sub": ACCOUNT_ID, "type": "access"}, SECRET, algorithm="HS256")
    with pytest.raises(HTTPException) as exc:
        get_caller(request_with({"Authorization": f"Bearer {token}"}), settings())
    assert exc.value.status_code == 401
