"""The agent's sandbox, seen from the backend.

/cablecore/api/{path}  Read-only gateway sandbox scripts call the cableCORE API through. A script holds
                       only a short-lived ticket (from the agent's cablecore_api_access tool), never the
                       user's token; only reads pass (GET, or a route the backend marks read-only).
                       Changes go through the agent's cc_action: validated, approved, read back.
/chat/files            Download of a file the agent made in the sandbox (replies link it as /sandbox/<path>).

ponytail: tickets live in this process (one worker); Valkey with a TTL when there are several.
"""

import mimetypes
import posixpath
import secrets
import time
from typing import Annotated, Any, Final

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status

from ai import sandbox
from ai.apps.cablecore.index import is_read_route
from ai.settings import settings as ai_settings
from app.auth import CurrentCaller

router = APIRouter(tags=["sandbox"])

TICKET_TTL_S: Final = 15 * 60
SANDBOX_WORK_DIR: Final = "/home/user/work/"
_tickets: dict[str, tuple[str, float]] = {}  # ticket -> (cableCORE token, expiry)


class Tickets:
    """The agent's TicketStore for one turn: tickets act with this user's token, read-only."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def issue(self, user_id: str, chat_id: str) -> dict[str, Any]:
        now = time.time()
        for key in [k for k, (_, exp) in _tickets.items() if exp < now]:
            del _tickets[key]
        ticket = secrets.token_urlsafe(24)
        _tickets[ticket] = (self._token, now + TICKET_TTL_S)
        return {"ticket": ticket, "expires_in_s": TICKET_TTL_S}


@router.api_route("/cablecore/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
async def gateway(path: str, request: Request, x_cc_ticket: Annotated[str, Header()] = "") -> Response:
    token, expiry = _tickets.get(x_cc_ticket, ("", 0.0))
    if not token or expiry < time.time():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No valid ticket. Get one from the cablecore_api_access tool and send it as X-CC-Ticket.")
    if not await is_read_route(request.method, path, token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "The sandbox has read-only access. Make changes with the cc_action tool.")
    headers = {"Authorization": f"Bearer {token}"}
    if request.headers.get("content-type"):
        headers["Content-Type"] = request.headers["content-type"]
    async with httpx.AsyncClient(timeout=300) as client:
        upstream = await client.request(request.method, f"{ai_settings().cc_api_url.rstrip('/')}/{path}",
                                        params=dict(request.query_params), content=await request.body() or None, headers=headers)
    return Response(upstream.content, status_code=upstream.status_code, media_type=upstream.headers.get("content-type"))


@router.get("/chat/files")
async def download(caller: CurrentCaller, path: Annotated[str, Query(min_length=2)]) -> Response:
    """Only files under the sandbox work folder; never the schema, the helpers or anything else."""
    clean = posixpath.normpath(path)
    if not clean.startswith(SANDBOX_WORK_DIR):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    try:
        name, content, mime = await sandbox.read_file(clean)
    except sandbox.SandboxError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found") from exc
    return Response(content, media_type=mime or mimetypes.guess_type(name)[0] or "application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
