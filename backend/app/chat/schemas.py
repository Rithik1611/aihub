"""Request/response models for the chat API.

The wire format is deliberately identical to cableCORE_BE's chat API - the
`{statusCode, message, data}` envelope and camelCase fields - so the existing
frontend can be pointed at aihub without changing a single hook, and so chat
history can be migrated from cableCORE verbatim. See docs/adr/0005.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

MAX_MESSAGE_LENGTH: Final = 4000
MAX_SESSION_NAME_LENGTH: Final = 100


class ChatSender(StrEnum):
    HUMAN = "human"
    AI = "ai"


class ChatRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ChatMode(StrEnum):
    """aihub serves DEFAULT only; the others stay in cableCORE_BE."""

    DEFAULT = "DEFAULT"
    FORMULA = "FORMULA"
    DESIGN = "DESIGN"
    SUPPORT = "SUPPORT"


class TaskStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class QuestionType(StrEnum):
    RADIO = "radio"
    CHECK = "check"


class _Camel(BaseModel):
    """camelCase on the wire, snake_case in Python. Both are accepted as input."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class _Request(_Camel):
    """Anything a client sends: an unknown field is a client bug, not something to drop."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class _Response(_Camel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


class _Payload(_Camel):
    """A payload that crosses a boundary we do not control (the AI service)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class ApiResponse[T](BaseModel):
    """cableCORE's response envelope, reproduced so the frontend is drop-in."""

    status_code: int = Field(serialization_alias="statusCode")
    message: str
    data: T


class TaskDetail(_Payload):
    label: str = ""
    meta: str = ""


class ChatTask(_Payload):
    """One backend change an AI turn made, shown as a task row above the reply."""

    key: str = ""
    label: str = ""
    amount: str = ""
    status: TaskStatus = TaskStatus.DONE
    details: list[TaskDetail] = Field(default_factory=list)


class ChatQuestion(_Payload):
    """A question the AI reply asks, shown as an approval card."""

    q: str = ""
    type: QuestionType = QuestionType.RADIO
    options: list[str] = Field(default_factory=list)
    # A short explanation under each option, same order (the frontend card shows it).
    descriptions: list[str] = Field(default_factory=list)


class ChatAttachment(_Payload):
    name: str
    size: int


class ChatMessageMetadata(_Payload):
    """Stored alongside a message. Clamped before persistence - see clamp_metadata."""

    state: dict[str, object] | None = None
    suggestions: list[str] | None = None
    attachment: ChatAttachment | None = None
    tasks: list[ChatTask] | None = None
    questions: list[ChatQuestion] | None = None
    # The reply without the part the approval card repeats.
    question_lead: str | None = None
    stopped: bool | None = None
    complete: bool | None = None


class AiResponsePayload(_Payload):
    """What the aihub AI service returns. Shape mirrors cableCORE's AI contract."""

    response: str | None = None
    suggestions: list[str] = Field(default_factory=list)
    state: dict[str, object] | None = None
    tasks: list[ChatTask] = Field(default_factory=list)
    questions: list[ChatQuestion] = Field(default_factory=list)
    question_lead: str | None = None
    cancelled: bool = False
    complete: bool = False


class SendMessage(_Request):
    message: Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)]
    session_id: uuid.UUID | None = None
    # Chosen by the client for a brand-new chat, so it knows the session even if it
    # stops waiting before the reply arrives.
    new_session_id: uuid.UUID | None = None
    mode: ChatMode = ChatMode.DEFAULT


class UpdateSessionName(_Request):
    name: Annotated[str, Field(min_length=1, max_length=MAX_SESSION_NAME_LENGTH)]


class PinSession(_Request):
    is_pinned: bool


class ChatReply(_Response):
    session_id: uuid.UUID
    reply: str
    mode: ChatMode = ChatMode.DEFAULT
    session_name: str | None = None
    suggestions: list[str] = Field(default_factory=list)
    user_message_index: int | None = None
    ai_message_index: int | None = None
    user_message_id: uuid.UUID | None = None
    ai_message_id: uuid.UUID | None = None
    state: dict[str, object] | None = None
    tasks: list[ChatTask] = Field(default_factory=list)
    questions: list[ChatQuestion] = Field(default_factory=list)
    question_lead: str | None = None
    stopped: bool | None = None
    complete: bool | None = None


class SessionSummary(_Response):
    """A row in the session list."""

    id: uuid.UUID
    name: str
    created_at: datetime
    updated_at: datetime
    message_count: int
    last_message: str | None = None
    is_pinned: bool
    suggestions: list[str] = Field(default_factory=list)


class SessionMessage(_Response):
    id: uuid.UUID
    session_id: uuid.UUID
    sender: ChatSender
    content: str
    created_at: datetime
    metadata: ChatMessageMetadata | None = None


class SessionMessages(_Response):
    session_id: uuid.UUID
    messages: list[SessionMessage]
    suggestions: list[str] = Field(default_factory=list)


class SessionName(_Response):
    session_id: uuid.UUID
    name: str


class SessionPin(_Response):
    session_id: uuid.UUID
    is_pinned: bool


class LastActive(_Response):
    session_id: uuid.UUID | None
