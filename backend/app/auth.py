import logging
from typing import Annotated, Final

import jwt
from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_BEARER_PREFIX: Final = "bearer "
# cableCORE refuses a refresh token on protected routes; aihub must refuse it too.
_REQUIRED_TOKEN_TYPE: Final = "access"
_UNAUTHORIZED: Final = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


class Caller(BaseModel):
    """The authenticated account, as carried by a cableCORE access token."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    account_id: str = Field(alias="sub", min_length=1)
    username: str | None = None
    role: str | None = None
    company_id: str | None = Field(default=None, alias="companyId")


def extract_token(request: Request, settings: Settings) -> str:
    header = request.headers.get("authorization")
    if header and header.lower().startswith(_BEARER_PREFIX):
        return header[len(_BEARER_PREFIX) :].strip()
    cookie = request.cookies.get(settings.jwt_cookie_name)
    if cookie:
        return cookie
    raise _UNAUTHORIZED


def get_caller(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Caller:
    """Verify a cableCORE-issued access token.

    aihub cannot reach cableCORE's Account table, so it checks only what the token
    itself proves: signature, expiry, token type and subject. Account state
    (deactivated, signed out, idle-timed-out) is NOT visible here - see
    docs/adr/0004.
    """
    token = extract_token(request, settings)
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        # Reason stays in the log; the client gets a generic 401.
        logger.info("token rejected: %s", type(exc).__name__)
        raise _UNAUTHORIZED from exc

    if payload.get("type") != _REQUIRED_TOKEN_TYPE:
        logger.info("token rejected: wrong type %r", payload.get("type"))
        raise _UNAUTHORIZED
    return Caller.model_validate(payload)


CurrentCaller = Annotated[Caller, Depends(get_caller)]
