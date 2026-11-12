"""Normal-chat endpoints.

aihub serves DEFAULT-mode chat only; SUPPORT and FORMULA stay in cableCORE_BE
(docs/adr/0004). The wire format matches cableCORE's chat API exactly, so the
existing frontend can be pointed here unchanged (docs/adr/0005).
"""

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Caller, CurrentCaller, extract_token
from app.chat import agent
from app.chat.repository import ChatRepository
from app.chat.schemas import (
    MAX_MESSAGE_LENGTH,
    ChatTask,
    ApiResponse,
    ChatMessageMetadata,
    ChatMode,
    ChatReply,
    ChatSender,
    LastActive,
    PinSession,
    SendMessage,
    SessionMessage,
    SessionMessages,
    SessionName,
    SessionPin,
    SessionSummary,
    UpdateSessionName,
)
from app.config import Settings, get_settings
from app.db import get_session
from app.routers.sandbox import Tickets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

SessionId = Annotated[uuid.UUID, Path(description="Chat session id")]

MAX_UPLOAD_BYTES = 20_000_000

_SESSION_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
)


def get_repository(session: Annotated[AsyncSession, Depends(get_session)]) -> ChatRepository:
    return ChatRepository(session)


Repository = Annotated[ChatRepository, Depends(get_repository)]
Config = Annotated[Settings, Depends(get_settings)]


def _account_id(caller_account_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(caller_account_id)
    except ValueError as exc:
        # A well-signed token whose subject is not an account id is not usable here.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        ) from exc


def _require_default(mode: ChatMode) -> None:
    """aihub stores normal chat only; anything else still belongs to cableCORE."""
    if mode is not ChatMode.DEFAULT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{mode.value} chat is not served by aihub.",
        )


async def _open_session(
    repo: ChatRepository, account_id: uuid.UUID, session_id: uuid.UUID | None, new_session_id: uuid.UUID | None
) -> tuple[uuid.UUID, bool]:
    """The turn's session: the caller's existing one, or a new one (with the id the client chose)."""
    if session_id is not None:
        if not await repo.is_session_owned(session_id, account_id):
            raise _SESSION_NOT_FOUND
        return session_id, False
    try:
        created = (await repo.create_session(account_id, new_session_id)).id
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This chat already exists. Reopen it to continue.",
        ) from exc
    await repo.commit()
    return created, True


async def _turn_events(
    request: Request,
    caller: Caller,
    repo: ChatRepository,
    settings: Settings,
    message: str,
    session_id: uuid.UUID | None,
    new_session_id: uuid.UUID | None,
    page: str,
    file: UploadFile | None,
) -> AsyncIterator[dict[str, Any]]:
    """One request of a chat turn on the agent (docs/adr/0006), as the frontend's stream events."""
    account_id = _account_id(caller.account_id)
    token = extract_token(request, settings)
    attachment = None
    if file is not None and file.filename:
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="The file is too large.")
        attachment = (file.filename, content, file.content_type or "application/octet-stream")
    session, new_session = await _open_session(repo, account_id, session_id, new_session_id)
    try:
        events = agent.respond(
            account_id=account_id, username=caller.username or caller.account_id, token=token, session_id=session,
            new_session=new_session, message=message, page=page, attachment=attachment, tickets=Tickets(token),
            stub=settings.ai_stub,
        )
        first = await anext(events)
    except agent.TurnBusy as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A reply is still being written in this chat."
        ) from exc
    yield first
    async for event in events:
        yield event


async def _final(events: AsyncIterator[dict[str, Any]]) -> ApiResponse[ChatReply]:
    """The blocking form of a turn: everything up to `done`."""
    async for event in events:
        if event["type"] == "done":
            return ApiResponse(
                status_code=status.HTTP_200_OK,
                message="Message processed successfully",
                data=ChatReply.model_validate({k: v for k, v in event.items() if k != "type"}),
            )
        if event["type"] == "error":
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=event["message"])
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="AI service is currently unavailable.")


@router.post("/send", response_model=ApiResponse[ChatReply])
async def send_message(
    body: SendMessage, request: Request, caller: CurrentCaller, repo: Repository, settings: Config
) -> ApiResponse[ChatReply]:
    """Store the user's message, run the turn on the agent, store the reply. When the agent needs an
    approval or an answer, the reply carries it as `questions` and the next message answers it."""
    _require_default(body.mode)
    return await _final(_turn_events(request, caller, repo, settings, body.message, body.session_id,
                                     body.new_session_id, "", None))


@router.post("/send-with-file", response_model=ApiResponse[ChatReply])
async def send_with_file(
    request: Request,
    caller: CurrentCaller,
    repo: Repository,
    settings: Config,
    message: Annotated[str, Form(max_length=MAX_MESSAGE_LENGTH)] = "",
    session_id: Annotated[uuid.UUID | None, Form(alias="sessionId")] = None,
    new_session_id: Annotated[uuid.UUID | None, Form(alias="newSessionId")] = None,
    page: Annotated[str, Form(max_length=500)] = "",
    file: Annotated[UploadFile | None, File()] = None,
) -> ApiResponse[ChatReply]:
    return await _final(_turn_events(request, caller, repo, settings, message or "(see the attached file)",
                                     session_id, new_session_id, page, file))


@router.post("/stream")
async def stream_message(
    request: Request,
    caller: CurrentCaller,
    repo: Repository,
    settings: Config,
    message: Annotated[str, Form(max_length=MAX_MESSAGE_LENGTH)] = "",
    session_id: Annotated[uuid.UUID | None, Form(alias="sessionId")] = None,
    new_session_id: Annotated[uuid.UUID | None, Form(alias="newSessionId")] = None,
    page: Annotated[str, Form(max_length=500)] = "",
    file: Annotated[UploadFile | None, File()] = None,
) -> StreamingResponse:
    """The same turn as server-sent events: `session`, `steps` (task rows), `delta` / `note` (text as it is
    written), then `done` (the stored reply, as /send returns it) or `error`."""
    if not message and file is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="message or file is required")
    events = _turn_events(request, caller, repo, settings, message or "(see the attached file)",
                          session_id, new_session_id, page, file)
    first = await anext(events)  # errors before the stream starts (auth, session, busy) stay HTTP errors
    session = uuid.UUID(first["sessionId"])

    async def body() -> AsyncIterator[str]:
        finished = False
        try:
            yield f"data: {json.dumps(first)}\n\n"
            async for event in events:
                yield f"data: {json.dumps(event, default=str)}\n\n"
                finished = event["type"] in ("done", "error")
        finally:
            if not finished:  # the user stopped or left mid-reply
                agent.cancel(session)

    return StreamingResponse(body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/progress/{session_id}", response_model=ApiResponse[dict[str, list[ChatTask]]])
async def progress(session_id: SessionId, caller: CurrentCaller, repo: Repository) -> ApiResponse[dict[str, list[ChatTask]]]:
    """Live task rows of the session's running turn, for clients on the blocking /send."""
    if not await repo.is_session_owned(session_id, _account_id(caller.account_id)):
        raise _SESSION_NOT_FOUND
    return ApiResponse(status_code=status.HTTP_200_OK, message="Progress retrieved successfully",
                       data={"tasks": agent.running_tasks(session_id)})


@router.get("/sessions", response_model=ApiResponse[list[SessionSummary]])
async def list_sessions(
    caller: CurrentCaller,
    repo: Repository,
    mode: ChatMode = ChatMode.DEFAULT,
) -> ApiResponse[list[SessionSummary]]:
    _require_default(mode)
    rows = await repo.find_user_sessions(_account_id(caller.account_id))
    return ApiResponse(
        status_code=status.HTTP_200_OK,
        message="User sessions retrieved successfully",
        data=[
            SessionSummary(
                id=session.id,
                name=session.name,
                created_at=session.started_at,
                updated_at=session.updated_at,
                message_count=count,
                last_message=last_message,
                is_pinned=session.is_pinned,
                suggestions=session.suggestions,
            )
            for session, count, last_message in rows
        ],
    )


@router.get("/sessions/{session_id}/messages", response_model=ApiResponse[SessionMessages])
async def get_session_messages(
    session_id: SessionId, caller: CurrentCaller, repo: Repository
) -> ApiResponse[SessionMessages]:
    session = await repo.find_owned_session_with_messages(
        session_id, _account_id(caller.account_id)
    )
    if session is None:
        raise _SESSION_NOT_FOUND
    return ApiResponse(
        status_code=status.HTTP_200_OK,
        message="Session messages retrieved successfully",
        data=SessionMessages(
            session_id=session.id,
            messages=[
                SessionMessage(
                    id=message.id,
                    session_id=message.session_id,
                    sender=ChatSender(message.sender),
                    content=message.content,
                    created_at=message.created_at,
                    metadata=(
                        ChatMessageMetadata.model_validate(message.message_metadata)
                        if message.message_metadata is not None
                        else None
                    ),
                )
                for message in session.messages
            ],
            suggestions=session.suggestions,
        ),
    )


@router.delete("/sessions/{session_id}", response_model=ApiResponse[None])
async def delete_session(
    session_id: SessionId, caller: CurrentCaller, repo: Repository
) -> ApiResponse[None]:
    account_id = _account_id(caller.account_id)
    if await repo.delete_owned_session(session_id, account_id) == 0:
        raise _SESSION_NOT_FOUND
    await repo.clear_last_active_session(account_id, session_id)
    return ApiResponse(
        status_code=status.HTTP_200_OK, message="Session deleted successfully", data=None
    )


@router.patch("/sessions/{session_id}/name", response_model=ApiResponse[SessionName])
async def rename_session(
    session_id: SessionId, body: UpdateSessionName, caller: CurrentCaller, repo: Repository
) -> ApiResponse[SessionName]:
    if await repo.rename_owned_session(session_id, _account_id(caller.account_id), body.name) == 0:
        raise _SESSION_NOT_FOUND
    return ApiResponse(
        status_code=status.HTTP_200_OK,
        message="Session name updated successfully",
        data=SessionName(session_id=session_id, name=body.name),
    )


@router.patch("/sessions/{session_id}/pin", response_model=ApiResponse[SessionPin])
async def pin_session(
    session_id: SessionId, body: PinSession, caller: CurrentCaller, repo: Repository
) -> ApiResponse[SessionPin]:
    updated = await repo.set_owned_session_pinned(
        session_id, _account_id(caller.account_id), body.is_pinned
    )
    if updated == 0:
        raise _SESSION_NOT_FOUND
    return ApiResponse(
        status_code=status.HTTP_200_OK,
        message="Session pin status updated successfully",
        data=SessionPin(session_id=session_id, is_pinned=body.is_pinned),
    )


@router.get("/last-active", response_model=ApiResponse[LastActive])
async def last_active_session(
    caller: CurrentCaller,
    repo: Repository,
    mode: ChatMode | None = None,
) -> ApiResponse[LastActive]:
    """The session the caller was last in, if it still exists and is still theirs."""
    if mode is not None:
        _require_default(mode)
    account_id = _account_id(caller.account_id)
    candidate = await repo.find_last_active_session_id(account_id)
    if candidate is None or not await repo.is_session_owned(candidate, account_id):
        candidate = None
    return ApiResponse(
        status_code=status.HTTP_200_OK,
        message="Last active session retrieved successfully",
        data=LastActive(session_id=candidate),
    )
