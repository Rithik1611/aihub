"""The agent <-> backend contract, as Pydantic models.

TurnContext: what the backend hands the agent for one turn (the agent keeps no per-user state of its own).
ai.run_turn(ctx, history, message) yields TurnEvent models in order. The backend forwards them to the FE
(`event.model_dump_json()` per SSE/WebSocket message) and stores the `items` event's items in the chat
history; the next turn gets that history back. Cards are what the agent asks the user: the backend
waits for the answer in ctx.wait_answer(card_id, card).

The loop itself works on plain dicts (`Event`); ai.run_turn validates every one into a TurnEvent.
"""

import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, Awaitable, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictBool, TypeAdapter

Event = dict[str, Any]  # an event as the loop builds it; validated into TurnEvent at the boundary


class _Model(BaseModel):
    model_config = ConfigDict(extra='ignore')


# ---- cards: the agent waits on ctx.ask(card) -> Answer


class ApprovalItem(_Model):
    summary: str  # what the change does, one line
    method: str
    path: str  # filled path, e.g. /design/123/copy
    args: str  # path/query/body/file as readable JSON (dropdown values unwrapped)


class ApprovalCard(_Model):
    kind: Literal['approval'] = 'approval'
    title: str  # "Approve 2 changes in cableCORE"
    items: list[ApprovalItem]
    markdown: str  # the same list rendered as markdown, for a simple card


class QuestionOption(_Model):
    label: Annotated[str, Field(min_length=1, max_length=80)]
    description: Annotated[str, Field(min_length=1, max_length=240)]


class Question(_Model):
    id: Annotated[str, Field(min_length=1, max_length=64)]
    header: Annotated[str, Field(max_length=48)] = ''
    question: Annotated[str, Field(min_length=1, max_length=500)]
    options: Annotated[list[QuestionOption], Field(min_length=2, max_length=3)]  # the recommended one first
    allow_other: bool = True  # free-text answer allowed


class QuestionCard(_Model):
    kind: Literal['question'] = 'question'
    questions: Annotated[list[Question], Field(min_length=1, max_length=3)]
    timeout_ms: Annotated[int, Field(ge=60_000, le=240_000)] = 120_000


Card = Annotated[ApprovalCard | QuestionCard, Field(discriminator='kind')]


class Answer(_Model):
    """The user's answer, from the FE through the backend. Strict: unknown fields are rejected and an
    approval is only ever the boolean True."""

    model_config = ConfigDict(extra='forbid')

    approved: StrictBool | None = None  # approval card
    status: Literal['answered', 'cancelled', 'timeout'] | None = None  # question card
    answers: dict[str, str] = {}  # question card: question id -> chosen label or free text


# ---- events a turn yields


class TextDelta(_Model):
    type: Literal['text.delta']
    text: str


class ToolStart(_Model):
    type: Literal['tool.start']
    call_id: str
    name: str
    args: dict[str, Any]


class ToolEnd(_Model):
    type: Literal['tool.end']
    call_id: str
    name: str
    status: Literal['completed', 'failed']
    result: str  # short preview for the UI; the model gets the full (capped) result


class CardEvent(_Model):
    type: Literal['card']
    card_id: str  # the backend resolves ctx.wait_answer(card_id, card) with the user's answer
    card: Card


class Check(_Model):
    type: Literal['check']
    kind: Literal['writes', 'coverage']  # reply vs the changes made / reply vs the request
    note: str  # markdown shown under the reply; also in the `items` of the turn (the model sees it next turn)


class Items(_Model):
    type: Literal['items']
    items: list[dict[str, Any]]  # Responses API items (messages, function calls + outputs, reasoning) for the history


class Done(_Model):
    type: Literal['done']
    usage: dict[str, int]


class Error(_Model):
    type: Literal['error']
    message: str


TurnEvent = Annotated[TextDelta | ToolStart | ToolEnd | CardEvent | Check | Items | Done | Error, Field(discriminator='type')]
EVENTS: TypeAdapter[TurnEvent] = TypeAdapter(TurnEvent)


# ---- what a write harness logs per change (for the after-turn check)


class WriteRecord(_Model):
    at: float
    action: str  # route id
    risk: str  # read | write | destroy
    method: str
    path: str
    query: dict[str, Any]
    request: str  # body / file, clipped
    status: str  # done | failed | not run ...
    response: str  # error, or result + job state, clipped
    after: str | None = None  # the affected records as stored afterwards (on the batch's last entry)


# ---- turn context


class TicketStore(Protocol):
    """Short-lived read tickets for sandbox scripts (backend-owned, e.g. Valkey with a TTL)."""

    async def issue(self, user_id: str, chat_id: str) -> dict[str, Any]:
        """-> {'ticket': str, 'expires_in_s': int}"""
        ...


def _no_emit(event: Event) -> None:
    pass


@dataclass
class TurnContext:
    user_id: str
    user_email: str
    cc_token: str  # the user's cableCORE JWT; every cableCORE call runs with the user's own permissions
    chat_id: str
    # Wait for the user's answer to this card (it also goes out as a `card` event, which may reach the
    # backend after this call starts: register the wait by card_id here). The backend keeps the pending
    # card (so a reload can still answer it) and applies the timeout (then answer {}: not approved).
    # It may return an Answer or a dict; either is validated strictly.
    wait_answer: Callable[[str, Card], Awaitable[Answer | dict[str, Any]]]
    tickets: TicketStore | None = None
    app_id: str = 'cablecore'
    # Changes the write harness made in this turn, for the after-turn check. Lives and dies with the turn.
    writes: list[WriteRecord] = field(default_factory=lambda: list[WriteRecord]())
    # Set by the loop: puts an event on the turn's stream.
    emit: Callable[[Event], None] = field(default=_no_emit, repr=False)

    async def ask(self, card: ApprovalCard | QuestionCard) -> Answer:
        """Send a card on the turn's stream and wait for the user's answer (raises on a malformed answer)."""
        card_id = uuid.uuid4().hex
        self.emit({'type': 'card', 'card_id': card_id, 'card': card.model_dump()})
        return Answer.model_validate(await self.wait_answer(card_id, card) or {})


def user_message(text: str, attachment: tuple[str, str] | None = None, screen: str = '') -> dict[str, Any]:
    """The user's message as the model sees it. attachment = (file name, sandbox path) of a file the
    backend uploaded to the sandbox; screen = the DesignCenter page the user is on, so "this design" resolves."""
    if attachment:
        text += f'\n\n[Attached file "{attachment[0]}" is at {attachment[1]}]'
    text += f'\n\n[DesignCenter screen: {screen or "/"}]'
    return {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}
