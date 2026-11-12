"""Client for the aihub AI service.

The request/response shape mirrors cableCORE's AI contract, so the engine can be
swapped in without touching the storage layer. The service is still under
development; when it is unreachable the caller keeps the stored human message and
surfaces the same error cableCORE does.
"""

import logging
from typing import Final

import httpx
from pydantic import ValidationError

from app.chat.schemas import AiResponsePayload, ChatRole, ChatSender

logger = logging.getLogger(__name__)

# Above the engine's turn budget and below the frontend's, so the engine answers or
# gives up first instead of the request timing out mid-way.
AI_TIMEOUT_SECONDS: Final = 570.0


class AiServiceUnavailable(RuntimeError):
    """The AI service could not be reached or returned something unusable."""


class ConversationTurn(dict[str, str]):
    pass


def build_history(
    messages: list[tuple[str, str]], current_message: str | None = None
) -> list[dict[str, str]]:
    """Transcript tail the engine reasons over, oldest first.

    An empty current turn is still appended, so the engine never re-reads the
    previous one as if it were new.
    """
    history = [
        {
            "role": (
                ChatRole.USER if sender == ChatSender.HUMAN.value else ChatRole.ASSISTANT
            ).value,
            "content": content,
        }
        for sender, content in messages
    ]
    if current_message is not None:
        history.append({"role": ChatRole.USER.value, "content": current_message})
    return history


class AiClient:
    def __init__(self, base_url: str, access_token: str | None = None) -> None:
        self._url = base_url.rstrip("/")
        self._token = access_token

    async def send(
        self, session_id: str, history: list[dict[str, str]]
    ) -> AiResponsePayload:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        payload = {"sessionId": session_id, "messages": history, "mode": "DEFAULT"}
        try:
            async with httpx.AsyncClient(timeout=AI_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{self._url}/chat", json=payload, headers=headers
                )
                response.raise_for_status()
                return AiResponsePayload.model_validate(response.json())
        except (httpx.HTTPError, ValidationError, ValueError) as exc:
            # The reason stays in the log; the client gets a generic message.
            logger.warning("AI service call failed: %s: %s", type(exc).__name__, exc)
            raise AiServiceUnavailable from exc


STUB_PREFIX: Final = "[stub reply - AI service not connected]"


def stub_response(message: str) -> AiResponsePayload:
    """Canned answer used when `ai_stub` is on, so the chat flow is testable
    before the AI service exists. The prefix makes stored stub content obvious in
    the database and on screen."""
    return AiResponsePayload(
        response=f"{STUB_PREFIX} You asked: {message}",
        suggestions=["Ask something else", "Start a new chat"],
        complete=True,
    )
