import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ai import http as ai_http
from ai import settings as ai_settings
from app.config import get_settings
from app.routers import chat, health, sandbox

settings = get_settings()
logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO)

if settings.ai_stub and not settings.debug:
    # A canned reply must never reach a real user.
    raise RuntimeError("AI_STUB is a development setting and requires DEBUG=true")

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if not settings.ai_stub:
        ai_settings.require()  # the agent's keys: fail at startup, not in the middle of a chat
    yield
    await ai_http.close()


app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """Details go to the log; the client gets nothing that describes the failure."""
    logging.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.include_router(health.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(sandbox.router, prefix="/api")
