"""The agent's data tools over HTTP, for Code Mode's `dc` connector in cableCORE_BE (CC_TOOL_MODE=codemode_all).

POST /internal/tools/{cc_search|cc_get|cc_related|cc_formula} with the tool's arguments as the JSON body;
the answer is exactly what the agent's tool returns. The caller's cableCORE JWT is verified like every
chat request and the tool runs as that user. Only callers on this machine are served (the Code Mode
worker runs next to cableCORE_BE on the same host); widen INTERNAL_TOOL_HOSTS for a split deployment.
"""

import json
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import Response

from ai.apps.cablecore.tools import cc_formula, cc_get, cc_related, cc_search
from ai.context import Answer, Card, TurnContext
from ai.tools import Tool, run, tool
from app.auth import CurrentCaller, extract_token
from app.config import Settings, get_settings

router = APIRouter(prefix="/internal/tools", tags=["internal"], include_in_schema=False)

INTERNAL_TOOL_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})
DATA_TOOLS: Final[dict[str, Tool]] = {t.name: t for t in (tool(cc_search), tool(cc_get), tool(cc_related), tool(cc_formula))}


async def _no_cards(_card_id: str, _card: Card) -> Answer:
    # Data tools never ask the user anything.
    return Answer()


@router.post("/{name}")
async def run_data_tool(
    name: Annotated[str, Path(pattern=r"^cc_(search|get|related|formula)$")],
    request: Request,
    caller: CurrentCaller,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    if request.client is None or request.client.host not in INTERNAL_TOOL_HOSTS:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Internal endpoint.")
    try:
        args: Any = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Body must be JSON.") from exc
    ctx = TurnContext(
        user_id=caller.account_id,
        user_email=caller.username or caller.account_id,
        cc_token=extract_token(request, settings),
        chat_id="codemode",
        wait_answer=_no_cards,
    )
    outcome, text = await run(DATA_TOOLS, ctx, name, json.dumps(args if isinstance(args, dict) else {}))
    code = status.HTTP_200_OK if outcome == "completed" else status.HTTP_400_BAD_REQUEST
    return Response(content=text, status_code=code, media_type="application/json")
