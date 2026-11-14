"""Builders the tests share.

`Settings` requires a database URL and a JWT secret, so every test that touches
configuration or authentication was constructing the same object with the same two
`type: ignore` comments. One builder keeps that in a single place, and keeps the
test files about what they are asserting.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

import jwt

from app.core.auth import Caller
from app.core.config import Settings

SECRET: Final = "test-secret-not-used-anywhere-real"  # a fixture, not a credential
ACCOUNT_ID: Final = uuid.UUID("6f1c2f2e-0b5a-4d6e-9f3a-2b7c8d9e0f11")
OTHER_ACCOUNT_ID: Final = uuid.UUID("11111111-2222-4333-8444-555555555555")
PLANT_ID: Final = uuid.UUID("62a941e0-907a-4282-8634-062d5562eef0")
COMPANY_ID: Final = uuid.UUID("c0ffee00-0000-4000-8000-000000000001")
ALLOWED_ORIGIN: Final = "http://localhost:3001"

_DATABASE_URL: Final = "postgresql+asyncpg://u:p@localhost:5432/db"


def make_settings(**overrides: object) -> Settings:
    """Settings with the required fields filled in; override only what a test is about."""
    values: dict[str, object] = {
        "database_url": _DATABASE_URL,
        "jwt_secret": SECRET,
        **overrides,
    }
    return Settings(**values)  # type: ignore[arg-type]  # values are env-shaped, not literals


def make_claims(**overrides: object) -> dict[str, object]:
    """The claims a cableCORE access token carries for a licensed caller."""
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": str(ACCOUNT_ID),
        "username": "rithik",
        "role": "DESIGNER",
        "companyId": str(COMPANY_ID),
        "activePlantId": str(PLANT_ID),
        "canAccessAi": True,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    claims.update(overrides)
    return claims


def make_token(**overrides: object) -> str:
    """A token signed with the test secret; overrides go straight into the claims."""
    return jwt.encode(make_claims(**overrides), SECRET, algorithm="HS256")


def make_caller(**overrides: object) -> Caller:
    """A Caller as get_caller would build it, for testing what comes after it."""
    return Caller.model_validate(make_claims(**overrides))
