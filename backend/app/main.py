"""Application wiring: lifespan, middleware, error handling, one router per module."""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from ai import http as ai_http
from ai import settings as ai_settings
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.chat.router import router as chat_router
from app.core.config import Settings, get_settings
from app.core.db import dispose_engine
from app.health.router import router as health_router
from app.sandbox.router import router as sandbox_router

logger = logging.getLogger(__name__)


def _configure(settings: Settings) -> None:
    logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO)
    if settings.ai_stub and not settings.debug:
        # A canned reply must never reach a real user.
        raise RuntimeError("AI_STUB is a development setting and requires DEBUG=true")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the agent's and the database's resources for the life of the app.

    Without the dispose, pooled connections stay open through shutdown and reload,
    leaking server-side sessions until Postgres times them out.
    """
    if not settings.ai_stub:
        ai_settings.require()  # the agent's keys: fail at startup, not mid-chat
    yield
    await ai_http.close()
    await dispose_engine()


settings = get_settings()
_configure(settings)

# The schema names every route, including the sandbox's cableCORE gateway, and is a
# map of the attack surface. Served in development only; openapi_url=None also turns
# off /docs and /redoc.
app = FastAPI(
    title=settings.app_name,
    debug=settings.debug,
    lifespan=lifespan,
    openapi_url="/openapi.json" if settings.debug else None,
)

# Explicit, not "*": with credentials allowed, every method and header listed here is
# something another allowed origin may send with the user's cookie.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=["Retry-After"],
    max_age=600,
)


@app.middleware("http")
async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Responses carry a user's conversations, so nothing between aihub and the
    browser should sniff, frame or cache them."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """Details go to the log; the client gets nothing that describes the failure."""
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# One line per feature module; each owns its own prefix and tags.
app.include_router(health_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(sandbox_router, prefix="/api")
