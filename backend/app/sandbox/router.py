"""The agent's sandbox, seen from the backend.

/cablecore/api/{path}  Read-only gateway sandbox scripts call the cableCORE API through. A script holds
                       only a short-lived ticket (from the agent's cablecore_api_access tool), never the
                       user's token; only reads pass (GET, or a route the backend marks read-only).
                       Changes go through the agent's cc_action: validated, approved, read back.
/cablecore/rows        Table rows for scripts (cc rows), through the assistant's cc_related as the ticket's
                       user: the sandbox has no database login, so the chat's licence and plant rules hold.
/chat/files            Download of a file the agent made in the caller's own sandbox home (replies link it
                       as /sandbox/<path>).

ponytail: tickets live in this process (one worker); Valkey with a TTL when there are several.
"""

import mimetypes
import posixpath
import secrets
import time
from typing import Annotated, Any, Final

import httpx
import psycopg
from ai import sandbox
from ai.apps.cablecore.api import CableCoreError
from ai.apps.cablecore.index import is_read_route
from ai.apps.cablecore.tools import cc_related
from ai.context import Answer, Card, TurnContext
from ai.settings import settings as ai_settings
from ai.tools import tool
from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from pydantic import ValidationError

from app.core.auth import CurrentCaller

router = APIRouter(tags=["sandbox"])

TICKET_TTL_S: Final = 15 * 60
_tickets: dict[str, tuple[str, float]] = {}  # ticket -> (cableCORE token, expiry)
_ROWS: Final = tool(cc_related)  # validates arguments exactly as for the model


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


def _ticket_token(ticket: str) -> str:
    token, expiry = _tickets.get(ticket, ("", 0.0))
    if not token or expiry < time.time():
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "No valid ticket. Get one from the cablecore_api_access tool and send it as X-CC-Ticket.",
        )
    return token


async def _no_cards(card_id: str, card: Card) -> Answer:
    raise HTTPException(status.HTTP_403_FORBIDDEN, "The sandbox cannot ask the user.")


@router.post("/cablecore/rows", include_in_schema=False)
async def rows(request: Request, x_cc_ticket: Annotated[str, Header()] = "") -> Response:
    """Table rows for sandbox scripts: the assistant's own cc_related as the ticket's user, so the same licence,
    plant and table rules apply as in the chat. The sandbox has no database login of its own."""
    token = _ticket_token(x_cc_ticket)
    related = _ROWS.args_model
    if related is None:  # cc_related is a Python tool: it always has its argument model
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "rows are not available")
    try:
        args = related.model_validate(await request.json())
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)[:2000]) from exc
    ctx = TurnContext(user_id="", user_email="", cc_token=token, chat_id="", wait_answer=_no_cards)
    try:
        text = await _ROWS.fn(ctx, **args.model_dump(exclude_unset=True))
    except CableCoreError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except psycopg.Error as exc:  # a filter that does not fit the column (e.g. text for a uuid): the script's mistake
        reason = str(exc).splitlines()[0][:300]
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"The query failed: {reason}") from exc
    return Response(str(text), media_type="application/json" if str(text).startswith("{") else "text/plain")


@router.api_route(
    "/cablecore/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False
)
async def gateway(path: str, request: Request, x_cc_ticket: Annotated[str, Header()] = "") -> Response:
    token = _ticket_token(x_cc_ticket)
    if not await is_read_route(request.method, path, token):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "The sandbox has read-only access. Make changes with the cc_action tool."
        )
    headers = {"Authorization": f"Bearer {token}"}
    if request.headers.get("content-type"):
        headers["Content-Type"] = request.headers["content-type"]
    async with httpx.AsyncClient(timeout=300) as client:
        upstream = await client.request(
            request.method,
            f"{ai_settings().cc_api_url.rstrip('/')}/{path}",
            params=dict(request.query_params),
            content=await request.body() or None,
            headers=headers,
        )
    return Response(upstream.content, status_code=upstream.status_code, media_type=upstream.headers.get("content-type"))


@router.get("/chat/files")
async def download(caller: CurrentCaller, path: Annotated[str, Query(min_length=2)]) -> Response:
    """Only files in the caller's own sandbox home; never another user's, the helpers or anything else."""
    clean = posixpath.normpath(path)
    user_id = str(caller.account_id)
    try:
        own = await sandbox.home(user_id)
        if not clean.startswith(own + "/"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
        name, content, mime = await sandbox.read_file(clean, user_id)
    except sandbox.SandboxError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found") from exc
    return Response(
        content,
        media_type=mime or mimetypes.guess_type(name)[0] or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
