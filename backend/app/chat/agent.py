"""DEFAULT-mode chat turns on the aihub agent (`ai/`), run in process (docs/adr/0009).

One chat turn = one `ai.run_turn`. Its events are turned into the events the frontend's chat stream
already reads (`session`, `steps`, `delta`, `note`, `done`, `error`) and the reply is stored like
any other. When the agent needs the user (an approval card for a change, or a question), the
request ends with that card as `questions`, exactly like cableCORE's chat; the agent turn stays
paused, and the user's next message in the session is its answer and resumes the same turn.

The agent's own record of a turn (tool calls, results, encrypted reasoning) is stored on the turn's
final AI message and replayed as history, so later turns know what was looked up and changed.

ponytail: paused and running turns live in this process (one worker, or sticky sessions). Move the
registry to Valkey + a worker queue if aihub ever runs several workers per chat.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Final

import ai
from ai import sandbox
from ai.context import ApprovalCard, Card, CardEvent, Check, Error, Items, TextDelta, ToolEnd, ToolStart, TurnContext

from app.chat.models import ChatMessage
from app.chat.repository import ChatRepository
from app.chat.schemas import ChatMessageMetadata, ChatQuestion, ChatReply, ChatSender, ChatTask, TaskStatus
from app.core.db import SessionFactory

logger = logging.getLogger(__name__)

CARD_TIMEOUT_S: Final = 600  # an unanswered card declines; the turn then finishes on its own
HISTORY_MESSAGES: Final = 60  # newest stored messages the next turn is built from
FULL_TURNS: Final = 3  # newest turns replayed with their tool calls; older ones as plain text
ITEMS_MAX_BYTES: Final = 2_000_000
STUB_PREFIX: Final = "[stub reply - AI service not connected]"
APPROVE, DECLINE = "Approve", "Decline"


class TurnBusy(Exception):
    """The session already has a reply being written."""


@dataclass
class Turn:
    session_id: uuid.UUID
    account_id: uuid.UUID
    new_session: bool
    first_text: str
    events: asyncio.Queue[dict[str, Any]] = field(default_factory=lambda: asyncio.Queue[dict[str, Any]]())
    covered: list[str] = field(default_factory=lambda: list[str]())  # message ids the agent items already contain
    tasks: list[ChatTask] = field(default_factory=lambda: list[ChatTask]())
    text: str = ""  # answer text since the turn started or last resumed
    card: Card | None = None
    answer: "asyncio.Future[dict[str, Any]] | None" = None
    task: "asyncio.Task[None] | None" = None


_turns: dict[uuid.UUID, Turn] = {}


def claim(
    session_id: uuid.UUID, account_id: uuid.UUID, new_session: bool, message: str, *, stub: bool = False
) -> Turn | None:
    """Take the session's turn slot, or raise TurnBusy if a reply is already being written.

    The check and the registration must not be separated by an await. They used to be:
    the slot was taken only after the message was stored, so two requests arriving
    together both passed the check and ran turns that interleaved in the transcript.
    asyncio runs this synchronously, so claiming here is atomic.

    Returns the paused turn when one is waiting for an answer, a fresh claimed turn
    otherwise, and None for the stub path, which runs no turn.
    """
    existing = _turns.get(session_id)
    if existing is not None:
        if existing.answer is None:
            raise TurnBusy
        return existing  # paused on a card: this message is its answer
    if stub:
        return None
    turn = Turn(session_id, account_id, new_session, message)
    _turns[session_id] = turn
    return turn


def running_tasks(session_id: uuid.UUID) -> list[ChatTask]:
    """Live task rows of the session's turn (for /chat/progress)."""
    turn = _turns.get(session_id)
    return list(turn.tasks) if turn else []


# ---- history


def _text_item(role: str, text: str) -> dict[str, Any]:
    kind = "input_text" if role == "user" else "output_text"
    return {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}


def _compact(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """An older turn as its conversation only: no tool calls, results or reasoning."""
    return [i for i in items if i.get("type") == "message"]


def history_items(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """The agent's history from stored messages, oldest first. Turns the agent ran are replayed from
    their items (the newest FULL_TURNS in full); anything else (cableCORE-migrated history, stub
    replies) as plain text."""
    covered: set[str] = set()
    with_items: list[uuid.UUID] = []
    for m in messages:
        if m.agent_items:
            covered.update(str(c) for c in m.agent_items.get("covered") or [])  # type: ignore[attr-defined]
            with_items.append(m.id)
    full = set(with_items[-FULL_TURNS:])
    out: list[dict[str, Any]] = []
    for m in messages:
        if str(m.id) in covered:
            continue
        if m.agent_items:
            items: list[dict[str, Any]] = list(m.agent_items.get("items") or [])  # type: ignore[call-overload]
            out += items if m.id in full else _compact(items)
        else:
            out.append(_text_item("user" if m.sender == ChatSender.HUMAN.value else "assistant", m.content))
    return out


def _bounded(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keeps a stored turn under ITEMS_MAX_BYTES: long tool results are cut first, then tool calls go."""
    if len(json.dumps(items, default=str)) <= ITEMS_MAX_BYTES:
        return items
    cut = [
        {**i, "output": i["output"][:4000] + " …[cut]"}
        if i.get("type") == "function_call_output" and len(i.get("output", "")) > 4000
        else i
        for i in items
    ]
    return cut if len(json.dumps(cut, default=str)) <= ITEMS_MAX_BYTES else _compact(items)


# ---- events -> frontend


def _clip(value: object, size: int = 90) -> str:
    s = " ".join(str(value or "").split())
    return s if len(s) <= size else s[: size - 1] + "…"


def step_label(name: str, args: dict[str, Any]) -> str:
    """The task row text, as the DesignCenter chat panel words it."""
    if name == "cc_action":
        ids = [str(a.get("id", "")) for a in args.get("actions") or [] if isinstance(a, dict)]
        return f"Ran {ids[0] if len(ids) == 1 else f'{len(ids)} actions: ' + _clip(', '.join(ids), 70)}"
    labels: dict[str, Callable[[], str]] = {
        "run_command": lambda: f"Ran {_clip(args.get('command'))}",
        "search_web": lambda: f"Searched the web: {_clip(args.get('query'))}",
        "fetch_url": lambda: f"Read {_clip(args.get('url'))}",
        "delegate_task": lambda: f"Sub-agent: {_clip(args.get('task'))}",
        "cablecore_api_access": lambda: "Opened read access for the sandbox",
        "cc_search": lambda: f"Searched {args.get('kind', 'design')}s: {_clip(args.get('query'))}",
        "cc_get": lambda: f"Opened {args.get('kind', '')} {_clip(args.get('ref'), 40)}",
        "cc_related": lambda: f"Read {args.get('table', '')} rows {_clip(args.get('record'), 40)}".strip(),
        "cc_formula": lambda: f"Traced the formula of {_clip(args.get('attribute'), 40)}",
        "cc_find": lambda: f"Looked up how to: {_clip(args.get('query'))}",
        "cc_describe": lambda: f"Read the inputs of {_clip(args.get('id'), 60)}",
        "cc_note": lambda: f"Noted: {_clip(args.get('fact'))}",
        "ask_user": lambda: "Asked you a question",
    }
    return labels[name]() if name in labels else name.replace("_", " ")


def card_questions(card: Card) -> list[ChatQuestion]:
    if isinstance(card, ApprovalCard):
        return [
            ChatQuestion(
                q=card.title, options=[APPROVE, DECLINE], descriptions=["Run exactly these changes", "Change nothing"]
            )
        ]
    return [
        ChatQuestion(
            q=q.question, options=[o.label for o in q.options], descriptions=[o.description for o in q.options]
        )
        for q in card.questions
    ]


def answer_for(card: Card, text: str) -> dict[str, Any]:
    """The user's next message as the answer to the card (the frontend sends the chosen option's text,
    or 'question answer' lines when there were several questions)."""
    text = text.strip()
    if isinstance(card, ApprovalCard):
        return {"approved": text.lower().startswith(("approve", "yes", "ok", "go ahead", "confirm", "do it"))}
    answers: dict[str, str] = {}
    for q in card.questions:
        line = next((ln[len(q.question) :].strip() for ln in text.splitlines() if ln.startswith(q.question)), None)
        answers[q.id] = line if line else text
    return {"status": "answered", "answers": answers}


# ---- running a turn


async def _store_ai(
    turn: Turn,
    content: str,
    questions: list[ChatQuestion] | None,
    question_lead: str | None,
    agent_items: dict[str, object] | None,
) -> ChatReply:
    async with SessionFactory() as db:
        repo = ChatRepository(db)
        metadata = ChatMessageMetadata(
            tasks=list(turn.tasks) or None,
            questions=questions or None,
            question_lead=question_lead,
            complete=None if questions else True,
        )
        message = await repo.create_message(
            turn.session_id, ChatSender.AI, content or "Sorry, I could not generate a response.", metadata, agent_items
        )
        await repo.set_session_suggestions(turn.session_id, turn.account_id, [])
        if agent_items is not None and turn.new_session:
            await repo.rename_if_still_default(
                turn.session_id, turn.account_id, await ai.make_title(turn.first_text, content)
            )
        name = await repo.get_session_name(turn.session_id, turn.account_id)
        index = await repo.count_messages(turn.session_id) - 1
        await db.commit()
    return ChatReply(
        session_id=turn.session_id,
        reply=message.content,
        session_name=name,
        ai_message_index=index,
        ai_message_id=message.id,
        tasks=list(turn.tasks),
        questions=questions or [],
        question_lead=question_lead,
        complete=None if questions else True,
    )


async def _run(turn: Turn, ctx: TurnContext, history: list[dict[str, Any]], message: dict[str, Any]) -> None:
    put = turn.events.put_nowait
    items: list[dict[str, Any]] = []
    try:
        async for event in ai.run_turn(ctx, history, message):
            if isinstance(event, TextDelta):
                turn.text += event.text
                put({"type": "delta", "text": event.text})
            elif isinstance(event, ToolStart):
                if turn.text.strip():  # text before tool calls is a note, not the answer
                    put({"type": "note", "text": turn.text})
                    turn.text = ""
                turn.tasks.append(
                    ChatTask(key=event.call_id, label=step_label(event.name, event.args), status=TaskStatus.RUNNING)
                )
                put({"type": "steps", "tasks": [t.model_dump(by_alias=True) for t in turn.tasks]})
            elif isinstance(event, ToolEnd):
                for t in turn.tasks:
                    if t.key == event.call_id:
                        t.status = TaskStatus.DONE if event.status == "completed" else TaskStatus.FAILED
                put({"type": "steps", "tasks": [t.model_dump(by_alias=True) for t in turn.tasks]})
            elif isinstance(event, CardEvent):
                turn.card = event.card
                questions = card_questions(event.card)
                lead = "\n\n".join(
                    s
                    for s in (turn.text.strip(), event.card.markdown if isinstance(event.card, ApprovalCard) else "")
                    if s
                )
                reply = await _store_ai(turn, lead or questions[0].q, questions, lead or None, None)
                turn.covered.append(str(reply.ai_message_id))
                turn.text = ""
                put({"type": "paused", "reply": reply})
            elif isinstance(event, Check):
                turn.text += event.note
                put({"type": "delta", "text": event.note})
            elif isinstance(event, Items):
                items = event.items
            elif isinstance(event, Error):
                logger.warning("agent turn failed in session %s: %s", turn.session_id, event.message)
                turn.text = turn.text or "Sorry, something went wrong while answering. Please try again."
        reply = await _store_ai(
            turn, turn.text.strip(), None, None, {"covered": turn.covered, "items": _bounded(items)}
        )
        put({"type": "finished", "reply": reply})
    except Exception:
        logger.exception("agent turn crashed in session %s", turn.session_id)
        put({"type": "failed"})
    finally:
        _turns.pop(turn.session_id, None)


def _context(turn: Turn, caller_token: str, username: str, tickets: Any) -> TurnContext:
    async def wait_answer(card_id: str, card: Card) -> dict[str, Any]:
        turn.answer = asyncio.get_running_loop().create_future()
        try:
            return await asyncio.wait_for(turn.answer, CARD_TIMEOUT_S)
        except TimeoutError:
            return {}
        finally:
            turn.answer, turn.card = None, None

    return TurnContext(
        user_id=str(turn.account_id),
        user_email=username,
        cc_token=caller_token,
        chat_id=str(turn.session_id),
        wait_answer=wait_answer,
        tickets=tickets,
    )


async def respond(
    *,
    account_id: uuid.UUID,
    username: str,
    token: str,
    session_id: uuid.UUID,
    new_session: bool,
    message: str,
    page: str,
    attachment: tuple[str, bytes, str] | None,
    tickets: Any,
    stub: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """One request of a chat turn: the frontend's stream events, ending with `done` (a ChatReply) or `error`.
    The session must already exist and belong to the account."""
    turn = claim(session_id, account_id, new_session, message, stub=stub)
    resuming = turn is not None and turn.answer is not None
    try:
        async with SessionFactory() as db:
            repo = ChatRepository(db)
            history = (
                None
                if resuming
                else await repo.find_owned_session_recent_messages(session_id, account_id, HISTORY_MESSAGES)
            )
            meta = (
                ChatMessageMetadata.model_validate({"attachment": {"name": attachment[0], "size": len(attachment[1])}})
                if attachment
                else None
            )
            human = await repo.create_message(session_id, ChatSender.HUMAN, message, meta)
            await repo.set_last_active_session(account_id, session_id)
            index = await repo.count_messages(session_id) - 1
            await db.commit()
    except Exception:
        # The claim is only released by _run's finally once the task exists; until
        # then this request owns it, so a failure here must give the session back.
        if turn is not None and not resuming:
            _turns.pop(session_id, None)
        raise
    yield {"type": "session", "sessionId": str(session_id), "userMessageId": str(human.id), "userMessageIndex": index}

    if stub:
        stub_turn = Turn(session_id, account_id, new_session, message)
        reply = await _store_ai(stub_turn, f"{STUB_PREFIX} You asked: {message}", None, None, None)
        yield _done(reply, human.id, index)
        return

    assert turn is not None  # claim() only returns None for the stub path, handled above
    if resuming and turn.answer is not None and turn.card is not None:  # answer to a paused turn
        turn.covered.append(str(human.id))
        turn.answer.set_result(answer_for(turn.card, message))
    else:
        turn.covered.append(str(human.id))
        try:
            path = None
            if attachment:
                path = await sandbox.save_file(attachment[0], attachment[1], attachment[2], folder="uploads")
            user_item = ai.user_message(message, (attachment[0], path) if attachment and path else None, page)
            ctx = _context(turn, token, username, tickets)
            turn.task = asyncio.create_task(_run(turn, ctx, history_items(history or []), user_item))
        except Exception:
            _turns.pop(session_id, None)
            raise

    while True:
        event = await turn.events.get()
        if event["type"] in ("paused", "finished"):
            yield _done(event["reply"], human.id, index)
            return
        if event["type"] == "failed":
            yield {
                "type": "error",
                "status": 503,
                "message": "AI service is currently unavailable. Please try again later.",
            }
            return
        yield event


def cancel(session_id: uuid.UUID) -> None:
    """Stops the session's turn (the user pressed stop or left mid-stream); a paused turn is left to its timeout."""
    turn = _turns.get(session_id)
    if turn is not None and turn.answer is None and turn.task is not None:
        turn.task.cancel()


def _done(reply: ChatReply, user_message_id: uuid.UUID, user_index: int) -> dict[str, Any]:
    reply.user_message_id, reply.user_message_index = user_message_id, user_index
    return {"type": "done", **reply.model_dump(mode="json", by_alias=True)}
