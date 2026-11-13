"""Token verification is a trust boundary: every rejection path gets a test."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import HTTPException, Request

from app.core.auth import Caller, get_caller, require_ai_access
from app.core.config import Settings

SECRET = "test-secret-not-used-anywhere-real"
ACCOUNT_ID = "6f1c2f2e-0b5a-4d6e-9f3a-2b7c8d9e0f11"
PLANT_ID = "62a941e0-907a-4282-8634-062d5562eef0"


def settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",  # type: ignore[arg-type]
        jwt_secret=SECRET,  # type: ignore[arg-type]
    )


def make_token(**overrides: object) -> str:
    payload: dict[str, object] = {
        "sub": ACCOUNT_ID,
        "username": "rithik",
        "role": "DESIGNER",
        "companyId": "c0ffee00-0000-4000-8000-000000000001",
        "activePlantId": PLANT_ID,
        "canAccessAi": True,
        "type": "access",
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
    }
    payload.update(overrides)
    return jwt.encode(payload, SECRET, algorithm="HS256")


def request_with(
    headers: dict[str, str] | None = None,
    cookies: str | None = None,
    method: str = "GET",
) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookies:
        raw_headers.append((b"cookie", cookies.encode()))
    return Request({"type": "http", "headers": raw_headers, "method": method, "path": "/"})


def bearer(token: str | None = None, method: str = "GET") -> Request:
    return request_with({"Authorization": f"Bearer {token or make_token()}"}, method=method)


# --- token verification -------------------------------------------------------


async def test_accepts_bearer_token() -> None:
    caller = await get_caller(bearer(), settings())
    assert caller.account_id == uuid.UUID(ACCOUNT_ID)
    assert caller.active_plant_id == uuid.UUID(PLANT_ID)
    assert caller.can_access_ai is True


async def test_accepts_cookie_token() -> None:
    caller = await get_caller(request_with(cookies=f"access_token={make_token()}"), settings())
    assert caller.account_id == uuid.UUID(ACCOUNT_ID)


@pytest.mark.parametrize(
    ("name", "request_factory"),
    [
        ("missing token", lambda: request_with()),
        ("refresh token", lambda: bearer(make_token(type="refresh"))),
        (
            "expired beyond leeway",
            lambda: bearer(
                make_token(exp=int((datetime.now(UTC) - timedelta(minutes=5)).timestamp()))
            ),
        ),
        (
            "wrong secret",
            lambda: bearer(
                jwt.encode(
                    {"sub": ACCOUNT_ID, "type": "access", "exp": 9999999999},
                    "wrong-secret",
                    algorithm="HS256",
                )
            ),
        ),
        (
            "unsigned (alg=none)",
            lambda: bearer(
                jwt.encode(
                    {"sub": ACCOUNT_ID, "type": "access", "exp": 9999999999},
                    key="",
                    algorithm="none",
                )
            ),
        ),
        (
            "no subject",
            lambda: bearer(jwt.encode({"type": "access", "exp": 9999999999}, SECRET)),
        ),
        ("no expiry", lambda: bearer(jwt.encode({"sub": ACCOUNT_ID, "type": "access"}, SECRET))),
        (
            "subject is not a uuid",
            lambda: bearer(make_token(sub="not-an-account-id")),
        ),
    ],
)
async def test_rejects(name: str, request_factory: object) -> None:
    with pytest.raises(HTTPException) as exc:
        await get_caller(request_factory(), settings())  # type: ignore[operator]
    assert exc.value.status_code == 401, name


async def test_accepts_token_expired_within_clock_skew_leeway() -> None:
    """A few seconds of drift between cableCORE and aihub must not sign users out."""
    token = make_token(exp=int((datetime.now(UTC) - timedelta(seconds=5)).timestamp()))
    assert (await get_caller(bearer(token), settings())).account_id == uuid.UUID(ACCOUNT_ID)


# --- AI licence (docs/adr/0006) ----------------------------------------------


def licensed(**overrides: object) -> Caller:
    claims: dict[str, object] = {
        "sub": ACCOUNT_ID,
        "activePlantId": PLANT_ID,
        "canAccessAi": True,
    }
    claims.update(overrides)
    return Caller.model_validate(claims)


async def test_licensed_caller_is_allowed() -> None:
    caller = licensed()
    assert await require_ai_access(caller) is caller


async def test_rejects_caller_without_active_plant() -> None:
    with pytest.raises(HTTPException) as exc:
        await require_ai_access(licensed(activePlantId=None))
    assert exc.value.status_code == 403
    assert "active plant" in exc.value.detail


async def test_rejects_unlicensed_caller() -> None:
    """DCAdmin's real case: a valid token, no cableAI licence."""
    with pytest.raises(HTTPException) as exc:
        await require_ai_access(licensed(canAccessAi=False))
    assert exc.value.status_code == 403
    assert "not licensed" in exc.value.detail


async def test_token_without_licence_claims_is_unlicensed() -> None:
    """Fail closed: a token issued before the claims existed grants nothing."""
    caller = Caller.model_validate({"sub": ACCOUNT_ID})
    assert caller.can_access_ai is False
    with pytest.raises(HTTPException):
        await require_ai_access(caller)


# --- cross-site writes (docs/adr/0007) ---------------------------------------

ALLOWED = "http://localhost:3001"
COOKIE = f"access_token={make_token()}"


def cookie_write(
    method: str = "POST",
    origin: str | None = ALLOWED,
    content_type: str | None = "application/json",
) -> Request:
    headers: dict[str, str] = {}
    if origin is not None:
        headers["origin"] = origin
    if content_type is not None:
        headers["content-type"] = content_type
    return request_with(headers, cookies=f"access_token={make_token()}", method=method)


def allowed_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",  # type: ignore[arg-type]
        jwt_secret=SECRET,  # type: ignore[arg-type]
        cors_origins=[ALLOWED],
    )


async def test_cookie_write_from_allowed_origin_is_accepted() -> None:
    assert (await get_caller(cookie_write(), allowed_settings())).account_id == uuid.UUID(
        ACCOUNT_ID
    )


@pytest.mark.parametrize(
    ("name", "request_factory"),
    [
        ("foreign origin", lambda: cookie_write(origin="https://evil.example")),
        ("no origin", lambda: cookie_write(origin=None)),
        ("null origin (sandboxed iframe)", lambda: cookie_write(origin="null")),
        ("allowed host, wrong port", lambda: cookie_write(origin="http://localhost:9999")),
        (
            "allowed host, wrong scheme",
            lambda: cookie_write(origin="https://localhost:3001"),
        ),
        ("origin with trailing slash", lambda: cookie_write(origin=ALLOWED + "/")),
        (
            "form post (no preflight)",
            lambda: cookie_write(content_type="application/x-www-form-urlencoded"),
        ),
        (
            "text/plain post (no preflight)",
            lambda: cookie_write(content_type="text/plain"),
        ),
        (
            "multipart post (no preflight)",
            lambda: cookie_write(content_type="multipart/form-data"),
        ),
        ("post without content type", lambda: cookie_write(content_type=None)),
        (
            "foreign origin on delete",
            lambda: cookie_write(method="DELETE", origin="https://evil.example"),
        ),
        (
            "foreign origin on patch",
            lambda: cookie_write(method="PATCH", origin="https://evil.example"),
        ),
    ],
)
async def test_cookie_write_is_refused(name: str, request_factory: object) -> None:
    with pytest.raises(HTTPException) as exc:
        await get_caller(request_factory(), allowed_settings())  # type: ignore[operator]
    assert exc.value.status_code == 403, name


async def test_json_with_charset_is_accepted() -> None:
    request = cookie_write(content_type="application/json; charset=utf-8")
    assert (await get_caller(request, allowed_settings())).account_id == uuid.UUID(ACCOUNT_ID)


async def test_bodyless_delete_needs_no_content_type() -> None:
    """axios drops Content-Type when there is no body; DELETE always preflights anyway."""
    request = cookie_write(method="DELETE", content_type=None)
    assert (await get_caller(request, allowed_settings())).account_id == uuid.UUID(ACCOUNT_ID)


async def test_cookie_read_needs_no_origin() -> None:
    request = request_with(cookies=f"access_token={make_token()}", method="GET")
    assert (await get_caller(request, allowed_settings())).account_id == uuid.UUID(ACCOUNT_ID)


async def test_bearer_write_is_exempt() -> None:
    """A Bearer token is never attached by the browser on its own, so it cannot be
    used cross-site - no origin or content-type rule applies."""
    request = request_with(
        {"Authorization": f"Bearer {make_token()}", "origin": "https://anything.example"},
        method="POST",
    )
    assert (await get_caller(request, allowed_settings())).account_id == uuid.UUID(ACCOUNT_ID)


@pytest.mark.parametrize(
    "origin", ["*", "localhost:3001", "http://localhost:3001/app", "ftp://localhost", "http://"]
)
async def test_cors_origins_must_be_exact(origin: str) -> None:
    """The CORS list doubles as the write allowlist, so a loose entry is a config error."""
    with pytest.raises(ValueError):
        Settings(
            database_url="postgresql+asyncpg://u:p@localhost:5432/db",  # type: ignore[arg-type]
            jwt_secret=SECRET,  # type: ignore[arg-type]
            cors_origins=[origin],
        )


async def test_each_rejection_builds_its_own_exception() -> None:
    """A shared HTTPException instance would have `raise ... from` overwrite one
    request's error context with another's."""
    seen: list[int] = []
    for secret in ("wrong-one", "wrong-two"):
        token = jwt.encode(
            {"sub": ACCOUNT_ID, "type": "access", "exp": 9999999999}, secret, algorithm="HS256"
        )
        with pytest.raises(HTTPException) as exc:
            await get_caller(bearer(token), settings())
        seen.append(id(exc.value))
    assert seen[0] != seen[1]
