"""Authentication for cableCORE-issued access tokens.

aihub has no login of its own: it is used from inside cableCORE and verifies the
token cableCORE signs, with the shared `JWT_SECRET` (HS256). See docs/adr/0006.

Verifying a signature is not the same as authorising a request. What the token
proves (signature, expiry, type, subject) is checked here; what the token *claims*
(active plant, cableAI licence) is enforced by `require_ai_access`.
"""

import logging
import uuid
from typing import Annotated, Final

import jwt
from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

_BEARER_PREFIX: Final = "bearer "
# cableCORE refuses a refresh token on protected routes; aihub must refuse it too.
_REQUIRED_TOKEN_TYPE: Final = "access"  # noqa: S105  # a token type, not a secret
# Methods that change state. CSRF is only a risk for these.
_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Content types a chat write may legitimately carry: JSON for the API, multipart for
# the file upload and the streaming turn. `text/plain` and form encoding are still
# refused - no endpoint accepts them, and they are the shapes a cross-site form can
# send without a preflight.
_WRITE_TYPES: Final = frozenset({"application/json", "multipart/form-data"})


# Built per raise, never shared: `raise X from exc` sets __cause__ and __traceback__
# on the object, so one shared instance would have concurrent requests overwriting
# each other's error context.
def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _cross_site_refused() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request refused")


class Caller(BaseModel):
    """The authenticated account, as carried by a cableCORE access token."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    account_id: uuid.UUID = Field(alias="sub")
    username: str | None = None
    role: str | None = None
    company_id: uuid.UUID | None = Field(default=None, alias="companyId")
    # Licence claims, added by cableCORE at token-issue time (docs/adr/0006).
    # Absent on a token issued before that change, which is treated as no licence.
    active_plant_id: uuid.UUID | None = Field(default=None, alias="activePlantId")
    can_access_ai: bool = Field(default=False, alias="canAccessAi")


def extract_token(request: Request, settings: Settings) -> str:
    header = request.headers.get("authorization")
    if header and header.lower().startswith(_BEARER_PREFIX):
        return header[len(_BEARER_PREFIX) :].strip()
    cookie = request.cookies.get(settings.jwt_cookie_name)
    if cookie:
        return cookie
    raise _unauthorized()


def _from_cookie(request: Request) -> bool:
    header = request.headers.get("authorization")
    return not (header and header.lower().startswith(_BEARER_PREFIX))


def _check_cross_site(request: Request, settings: Settings) -> None:
    """Refuse a cookie-authenticated write that did not come from an allowed origin.

    A browser attaches the cookie to any request it sends to aihub, so on its own
    the cookie proves nothing about who started the request. Two browser-enforced
    facts do, and neither can be set by a page (docs/adr/0007):

    - `Origin` names the page that sent the request. It must match the allowlist
      exactly; a missing Origin on a cookie-authenticated write is refused. This is
      what actually stops CSRF: a page cannot set it.
    - A POST must carry a content type the API accepts (`_WRITE_TYPES`). Multipart
      is CORS-safelisted and so needs no preflight, but the Origin check above has
      already refused a foreign page; this only keeps shapes no endpoint serves,
      like a plain form post, from reaching a handler at all.

    No token, no shared secret, nothing for the client to fetch. A Bearer token is
    never attached by the browser on its own, so Bearer requests are exempt.
    """
    if request.method in _SAFE_METHODS or not _from_cookie(request):
        return
    origin = request.headers.get("origin")
    if origin not in settings.cors_origins:
        logger.warning(
            "cross-site write refused on %s %s: origin %r not allowed",
            request.method,
            request.url.path,
            origin,
        )
        raise _cross_site_refused()
    if request.method == "POST":
        media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if media_type not in _WRITE_TYPES:
            logger.warning(
                "cross-site write refused on POST %s: content-type %r",
                request.url.path,
                media_type,
            )
            raise _cross_site_refused()


async def get_caller(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Caller:
    """Verify a cableCORE access token and return who it belongs to.

    Account state (deactivated, signed out, idle-timed-out) lives in cableCORE's
    database, which aihub cannot read, so it is NOT revalidated here - see
    docs/adr/0006 for the window that leaves open.
    """
    _check_cross_site(request, settings)
    token = extract_token(request, settings)
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            leeway=settings.jwt_leeway_seconds,
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        # The reason stays in the log; the client gets a generic 401.
        logger.info("token rejected on %s: %s", request.url.path, type(exc).__name__)
        raise _unauthorized() from exc

    if payload.get("type") != _REQUIRED_TOKEN_TYPE:
        logger.warning("token rejected: wrong type %r", payload.get("type"))
        raise _unauthorized()
    try:
        return Caller.model_validate(payload)
    except ValueError as exc:
        # A well-signed token whose subject is not an account id is not usable.
        logger.warning("token rejected: unusable claims")
        raise _unauthorized() from exc


CurrentCaller = Annotated[Caller, Depends(get_caller)]


async def require_ai_access(caller: CurrentCaller) -> Caller:
    """cableAI is a licensed feature; the wording matches cableCORE's own 403s.

    shortcut: the licence is read from the token, so it is only as fresh as the
    token - up to the access-token lifetime. Switch to cableCORE's POST /mcp/whoami
    if a stale plant switch or a revoked session starts to matter (docs/adr/0006).
    """
    if caller.active_plant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Select an active plant to use cableAI.",
        )
    if not caller.can_access_ai:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cableAI is not licensed for your active plant.",
        )
    return caller


LicensedCaller = Annotated[Caller, Depends(require_ai_access)]
