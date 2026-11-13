"""Normal-chat endpoints.

aihub serves DEFAULT-mode chat only; SUPPORT and FORMULA stay in cableCORE_BE
(docs/adr/0004). The wire format matches cableCORE's chat API exactly, so the
existing frontend can be pointed here unchanged (docs/adr/0005).
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.ai_client import AiClient, AiServiceUnavailable, build_history, stub_response
from app.chat.repository import ChatRepository
from app.chat.schemas import (
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
from app.core.auth import extract_token
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.rate_limit import ThrottledCaller

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

SessionId = Annotated[uuid.UUID, Path(description="Chat session id")]

# The transcript tail sent to the engine. The engine keeps its own working memory,
# so only the newest turns matter.
HISTORY_WINDOW = 20

_SESSION_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
)


def get_repository(session: Annotated[AsyncSession, Depends(get_session)]) -> ChatRepository:
    return ChatRepository(session)


Repository = Annotated[ChatRepository, Depends(get_repository)]
Config = Annotated[Settings, Depends(get_settings)]


def _require_default(mode: ChatMode) -> None:
    """aihub stores normal chat only; anything else still belongs to cableCORE."""
    if mode is not ChatMode.DEFAULT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{mode.value} chat is not served by aihub.",
        )


@router.post("/send", response_model=ApiResponse[ChatReply])
async def send_message(
    body: SendMessage,
    request: Request,
    caller: ThrottledCaller,
    repo: Repository,
    settings: Config,
) -> ApiResponse[ChatReply]:
    """Store the user's message, ask the AI service, store the reply.

    The human message is written before the AI is called, so a failed or slow turn
    never loses what the user typed - the same order cableCORE uses.
    """
    _require_default(body.mode)
    account_id = caller.account_id

    if body.session_id is None:
        try:
            session_id = (await repo.create_session(account_id, body.new_session_id)).id
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This chat already exists. Reopen it to continue.",
            ) from exc
        recent: list[tuple[str, str]] = []
    else:
        history = await repo.find_owned_session_recent_messages(
            body.session_id, account_id, HISTORY_WINDOW
        )
        if history is None:
            raise _SESSION_NOT_FOUND
        session_id = body.session_id
        recent = [(m.sender, m.content) for m in history]

    await repo.set_last_active_session(account_id, session_id)
    user_message = await repo.create_message(session_id, ChatSender.HUMAN, body.message)
    user_index = await repo.count_messages(session_id) - 1
    # Committed before the AI call: a turn that fails or times out must not take the
    # user's message with it. cableCORE gets this from Prisma's per-query commit.
    await repo.commit()

    if settings.ai_stub:
        answer = stub_response(body.message)
    else:
        ai = AiClient(settings.ai_base_url, extract_token(request, settings))
        try:
            answer = await ai.send(str(session_id), build_history(recent, body.message))
        except AiServiceUnavailable as exc:
            # The user's message stays stored, exactly as in cableCORE.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AI service is currently unavailable. Please try again later.",
            ) from exc

    reply_text = answer.response or "Sorry, I could not generate a response."
    metadata = ChatMessageMetadata(
        state=answer.state,
        suggestions=answer.suggestions,
        tasks=answer.tasks or None,
        questions=answer.questions or None,
        question_lead=answer.question_lead if answer.questions else None,
        complete=answer.complete or None,
        stopped=answer.cancelled or None,
    )
    ai_message = await repo.create_message(session_id, ChatSender.AI, reply_text, metadata)
    await repo.set_session_suggestions(session_id, account_id, answer.suggestions)

    return ApiResponse(
        status_code=status.HTTP_200_OK,
        message="Message processed successfully",
        data=ChatReply(
            session_id=session_id,
            reply=reply_text,
            session_name=await repo.get_session_name(session_id, account_id),
            suggestions=answer.suggestions,
            user_message_index=user_index,
            ai_message_index=user_index + 1,
            user_message_id=user_message.id,
            ai_message_id=ai_message.id,
            state=answer.state,
            tasks=answer.tasks,
            questions=answer.questions,
            question_lead=answer.question_lead if answer.questions else None,
            complete=answer.complete or None,
            stopped=answer.cancelled or None,
        ),
    )


@router.get("/sessions", response_model=ApiResponse[list[SessionSummary]])
async def list_sessions(
    caller: ThrottledCaller,
    repo: Repository,
    mode: ChatMode = ChatMode.DEFAULT,
) -> ApiResponse[list[SessionSummary]]:
    _require_default(mode)
    rows = await repo.find_user_sessions(caller.account_id)
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
    session_id: SessionId, caller: ThrottledCaller, repo: Repository
) -> ApiResponse[SessionMessages]:
    session = await repo.find_owned_session_with_messages(session_id, caller.account_id)
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
    session_id: SessionId, caller: ThrottledCaller, repo: Repository
) -> ApiResponse[None]:
    account_id = caller.account_id
    if await repo.delete_owned_session(session_id, account_id) == 0:
        raise _SESSION_NOT_FOUND
    await repo.clear_last_active_session(account_id, session_id)
    return ApiResponse(
        status_code=status.HTTP_200_OK, message="Session deleted successfully", data=None
    )


@router.patch("/sessions/{session_id}/name", response_model=ApiResponse[SessionName])
async def rename_session(
    session_id: SessionId, body: UpdateSessionName, caller: ThrottledCaller, repo: Repository
) -> ApiResponse[SessionName]:
    if await repo.rename_owned_session(session_id, caller.account_id, body.name) == 0:
        raise _SESSION_NOT_FOUND
    return ApiResponse(
        status_code=status.HTTP_200_OK,
        message="Session name updated successfully",
        data=SessionName(session_id=session_id, name=body.name),
    )


@router.patch("/sessions/{session_id}/pin", response_model=ApiResponse[SessionPin])
async def pin_session(
    session_id: SessionId, body: PinSession, caller: ThrottledCaller, repo: Repository
) -> ApiResponse[SessionPin]:
    updated = await repo.set_owned_session_pinned(session_id, caller.account_id, body.is_pinned)
    if updated == 0:
        raise _SESSION_NOT_FOUND
    return ApiResponse(
        status_code=status.HTTP_200_OK,
        message="Session pin status updated successfully",
        data=SessionPin(session_id=session_id, is_pinned=body.is_pinned),
    )


@router.get("/last-active", response_model=ApiResponse[LastActive])
async def last_active_session(
    caller: ThrottledCaller,
    repo: Repository,
    mode: ChatMode | None = None,
) -> ApiResponse[LastActive]:
    """The session the caller was last in, if it still exists and is still theirs."""
    if mode is not None:
        _require_default(mode)
    account_id = caller.account_id
    candidate = await repo.find_last_active_session_id(account_id)
    if candidate is None or not await repo.is_session_owned(candidate, account_id):
        candidate = None
    return ApiResponse(
        status_code=status.HTTP_200_OK,
        message="Last active session retrieved successfully",
        data=LastActive(session_id=candidate),
    )
