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
import re
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import ai
import orjson
from ai import sandbox
from ai.context import (
    ApprovalCard,
    Card,
    CardEvent,
    Check,
    Correction,
    Done,
    Error,
    Items,
    TextDelta,
    ToolEnd,
    ToolStart,
    TurnContext,
)

from app.chat import files, memory, summary
from app.chat.models import ChatMessage
from app.chat.repository import ChatRepository, HistoryMessage
from app.chat.schemas import (
    ChatAnswer,
    ChatAttachment,
    ChatMessageMetadata,
    ChatQuestion,
    ChatReply,
    ChatSender,
    ChatTask,
    TaskDetail,
    TaskKind,
    TaskStatus,
)
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
    images: dict[str, uuid.UUID] = field(default_factory=lambda: dict[str, uuid.UUID]())  # data URL -> chat_file id
    answer: "asyncio.Future[dict[str, Any]] | None" = None
    task: "asyncio.Task[None] | None" = None


_turns: dict[uuid.UUID, Turn] = {}
_starting: set[uuid.UUID] = set()  # chats whose message is being taken in, until its reply is done
_tasks: set["asyncio.Task[None]"] = set()  # background work, referenced until done


def _background(work: "Coroutine[Any, Any, None]") -> None:
    task = asyncio.create_task(work)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


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


def history_items(messages: Sequence[ChatMessage | HistoryMessage]) -> list[dict[str, Any]]:
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


def _encoded(items: list[dict[str, Any]]) -> bytes:
    # orjson: a turn's record can be MBs, and it is encoded on every turn.
    return orjson.dumps(items, default=str, option=orjson.OPT_NON_STR_KEYS)


def _without_nul(items: list[dict[str, Any]], data: bytes) -> tuple[list[dict[str, Any]], bytes]:
    """NUL characters removed: Postgres cannot store them in text or JSONB (a tool may have read binary data)."""
    if b"\\u0000" not in data:
        return items, data
    data = data.replace(b"\\u0000", b"")
    return orjson.loads(data), data


def _storable(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _without_nul(items, _encoded(items))[0]


def _bounded(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keeps a stored turn under ITEMS_MAX_BYTES: long tool results are cut first, then tool calls go.
    Encoded once; again only when it has to be cut."""
    items, data = _without_nul(items, _encoded(items))
    if len(data) <= ITEMS_MAX_BYTES:
        return items
    cut = [
        {**i, "output": i["output"][:4000] + " …[cut]"}
        if i.get("type") == "function_call_output" and len(i.get("output", "")) > 4000
        else i
        for i in items
    ]
    return cut if len(_encoded(cut)) <= ITEMS_MAX_BYTES else _compact(items)


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
        "run_command": lambda: command_label(str(args.get("command") or "")),
        "search_web": lambda: f"Searched the web: {_clip(args.get('query'))}",
        "fetch_url": lambda: f"Read {_clip(args.get('url'))}",
        "delegate_task": lambda: f"Sub-agent: {_clip(args.get('task'))}",
        "cablecore_api_access": lambda: "Opened read access for the sandbox",
        "get_process_status": lambda: "Read the script's output",
        "send_process_input": lambda: "Sent input to the running script",
        "kill_process": lambda: "Stopped a running script",
        "list_processes": lambda: "Listed running scripts",
        "read_file": lambda: f"Read {_clip(args.get('path'), 60)}",
        "write_file": lambda: f"Wrote {_clip(args.get('path'), 60)}",
        "replace_file_content": lambda: f"Edited {_clip(args.get('path'), 60)}",
        "list_files": lambda: (
            f"Listed files in {_clip(args.get('path') or args.get('directory') or 'the workspace', 60)}"
        ),
        "grep_search": lambda: f"Searched files for {_clip(args.get('query') or args.get('pattern'), 50)}",
        "glob_search": lambda: f"Looked for files {_clip(args.get('pattern'), 50)}",
        "remember": lambda: "Saved a note to memory",
        "search_chats": lambda: f"Searched past chats: {_clip(args.get('query'), 60)}",
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


def command_label(command: str) -> str:
    """A sandbox command as a step a person reads: a script by what its first comment says it does,
    otherwise the command's first line."""
    lines = [ln.strip() for ln in command.strip().splitlines() if ln.strip()]
    first = lines[0] if lines else ""
    script = re.match(r"(?:uv run )?(python3?|node|bash|sh)\b", first)
    if script:
        comment = next(
            (ln.lstrip("# ").strip() for ln in lines[1:] if ln.startswith("#") and not ln.startswith("#!")), ""
        )
        kind = "Python" if script.group(1).startswith("python") else script.group(1)
        return f"Ran a {kind} script: {_clip(comment, 70)}" if comment else f"Ran a {kind} script"
    return f"Ran {_clip(first, 80)}"


TASK_KINDS: Final = {"search_web": TaskKind.SEARCH, "fetch_url": TaskKind.SEARCH, "run_command": TaskKind.CODING}
DETAIL_CHARS: Final = 480


def step_details(name: str, args: dict[str, Any]) -> list[TaskDetail]:
    """What the trace shows under a step when it starts: the command run or the page read."""
    if name == "run_command" and args.get("command"):
        return [TaskDetail(label=str(args["command"])[:DETAIL_CHARS], meta="command")]
    if name == "fetch_url" and args.get("url"):
        return [TaskDetail(label=str(args["url"])[:DETAIL_CHARS], meta="source")]
    return []


def result_details(name: str, result: str) -> list[TaskDetail]:
    """What the trace adds when a step ends: the pages a web search found, or the start of a command's output."""
    if name == "search_web":
        return [TaskDetail(label=link, meta="source") for link in re.findall(r'"link":\s*"([^"]+)"', result)[:8]]
    if name == "run_command" and (output := command_output(result).strip()):
        return [TaskDetail(label=output[:DETAIL_CHARS], meta="output")]
    return []


def command_output(result: str) -> str:
    """The printed text of a sandbox run_command result ({..., "output": [{"data": ...}]}). The result
    preview may be cut mid-JSON, so the data strings are read one by one."""
    chunks = re.findall(r'"data":\s*("(?:\\.|[^"\\])*")', result)
    try:
        return "".join(json.loads(c) for c in chunks).replace("\r\n", "\n") if chunks else result
    except ValueError:
        return result


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
            turn.session_id,
            ChatSender.AI,
            (content or "Sorry, I could not generate a response.").replace("\x00", ""),
            metadata,
            agent_items,
        )
        await repo.set_session_suggestions(turn.session_id, turn.account_id, [])
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


async def _title(turn: Turn, reply: str) -> None:
    """Names a new chat from its first exchange (DeepSeek, after the reply) and tells the open stream."""
    try:
        title = await ai.make_title(turn.first_text, reply)
        async with SessionFactory() as db:
            repo = ChatRepository(db)
            await repo.rename_if_still_default(turn.session_id, turn.account_id, title)
            name = await repo.get_session_name(turn.session_id, turn.account_id)
            await db.commit()
        turn.events.put_nowait({"type": "title", "sessionId": str(turn.session_id), "sessionName": name})
    except Exception:
        logger.exception("naming chat %s failed", turn.session_id)


async def _add_correction(turn: Turn, message_id: uuid.UUID, event: Correction) -> None:
    """A review after the turn found more: it goes under the stored reply and to the open stream."""
    async with SessionFactory() as db:
        repo = ChatRepository(db)
        await repo.append_to_message(
            message_id, turn.session_id, event.text.replace("\x00", ""), _storable(event.items)
        )
        await db.commit()
    turn.events.put_nowait({"type": "correction", "messageId": str(message_id), "text": event.text})


async def _run(turn: Turn, ctx: TurnContext, history: list[dict[str, Any]], message: dict[str, Any]) -> None:
    put = turn.events.put_nowait
    items: list[dict[str, Any]] = []
    stored: ChatReply | None = None
    title: asyncio.Task[None] | None = None
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
                    ChatTask(
                        key=event.call_id,
                        label=step_label(event.name, event.args),
                        status=TaskStatus.RUNNING,
                        kind=TASK_KINDS.get(event.name, TaskKind.STEP),
                        details=step_details(event.name, event.args),
                    )
                )
                put({"type": "steps", "tasks": [t.model_dump(by_alias=True) for t in turn.tasks]})
            elif isinstance(event, ToolEnd):
                for t in turn.tasks:
                    if t.key == event.call_id:
                        t.status = TaskStatus.DONE if event.status == "completed" else TaskStatus.FAILED
                        t.details += result_details(event.name, event.result)
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
            elif isinstance(event, Done):
                # The turn is done: stored and handed to the user now; the chat takes new messages again.
                stored = await _store_ai(
                    turn,
                    turn.text.strip(),
                    None,
                    None,
                    {"covered": turn.covered, "items": _bounded(files.without_image_data(items, turn.images))},
                )
                _turns.pop(turn.session_id, None)
                put({"type": "finished", "reply": stored})
                if turn.new_session:
                    title = asyncio.create_task(_title(turn, stored.reply))
            elif isinstance(event, Correction) and stored is not None and stored.ai_message_id is not None:
                await _add_correction(turn, stored.ai_message_id, event)
        if stored is None:
            raise RuntimeError("the turn ended without a reply")
        if title is not None:
            await title
        _background(summary.refresh(turn.session_id, HISTORY_MESSAGES))
    except Exception:
        logger.exception("agent turn crashed in session %s", turn.session_id)
        if stored is None:
            put({"type": "failed"})
    finally:
        _turns.pop(turn.session_id, None)
        put({"type": "closed"})


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
        memory=memory.Store(turn.account_id),
    )


async def respond(**kwargs: Any) -> AsyncIterator[dict[str, Any]]:
    """One request of a chat turn (see _respond). A chat takes one message at a time: the check and the claim
    happen together, before anything awaits, and the claim lasts until the reply is done."""
    session_id: uuid.UUID = kwargs["session_id"]
    turn = _turns.get(session_id)
    if session_id in _starting or (turn is not None and turn.answer is None):
        raise TurnBusy
    _starting.add(session_id)
    try:
        async for event in _respond(**kwargs):
            if event["type"] in ("done", "error"):
                _starting.discard(session_id)  # a new chat name or a review's correction may still follow
            yield event
    finally:
        _starting.discard(session_id)


async def _respond(
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
    answers: list[ChatAnswer] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """One request of a chat turn: the frontend's stream events, ending with `done` (a ChatReply) or `error`.
    The session must already exist and belong to the account."""
    turn = _turns.get(session_id)
    async with SessionFactory() as db:
        repo = ChatRepository(db)
        history = (
            None
            if turn
            else await repo.find_owned_session_history(session_id, account_id, HISTORY_MESSAGES, FULL_TURNS)
        )
        earlier = None if turn else await repo.get_session_summary(session_id, account_id)
        meta = (
            ChatMessageMetadata(
                attachment=ChatAttachment(name=attachment[0], size=len(attachment[1])) if attachment else None,
                answers=answers or None,
            )
            if attachment or answers
            else None
        )
        human = await repo.create_message(session_id, ChatSender.HUMAN, message, meta)
        await repo.set_last_active_session(account_id, session_id)
        index = await repo.count_messages(session_id) - 1
        await db.commit()
    yield {"type": "session", "sessionId": str(session_id), "userMessageId": str(human.id), "userMessageIndex": index}

    if stub:
        turn = Turn(session_id, account_id, new_session, message)
        reply = await _store_ai(turn, f"{STUB_PREFIX} You asked: {message}", None, None, None)
        yield _done(reply, human.id, index)
        return

    if turn is not None and turn.answer is not None and turn.card is not None:  # the answer to a paused turn
        turn.covered.append(str(human.id))
        turn.answer.set_result(answer_for(turn.card, message))
    else:
        turn = Turn(session_id, account_id, new_session, message, covered=[str(human.id)])
        path, document = None, ""
        images: list[dict[str, Any]] = []
        if attachment:
            try:
                path = await sandbox.save_file(
                    attachment[0], attachment[1], attachment[2], str(account_id), folder="uploads"
                )
            except sandbox.SandboxError as exc:
                logger.warning("upload %s not copied to the sandbox: %s", attachment[0], exc)
            row, problem = await files.store(account_id, session_id, *attachment)
            document = files.document_block(row, path, problem)
            if image := files.image_part(row, attachment[1]):
                images.append(image)
                turn.images[image["image_url"]] = row.id
        user_item = ai.user_message(
            message, (attachment[0], path) if attachment and path else None, page, document, images
        )
        ctx = _context(turn, token, username, tickets)
        _turns[session_id] = turn
        replay = await files.with_image_data(
            ([summary.summary_item(earlier)] if earlier else []) + history_items(history or [])
        )
        turn.task = asyncio.create_task(_run(turn, ctx, replay, user_item))

    while True:
        event = await turn.events.get()
        if event["type"] == "paused":
            yield _done(event["reply"], human.id, index)
            return
        if event["type"] == "finished":  # the reply; a new chat's name and a review's correction may follow
            yield _done(event["reply"], human.id, index)
            continue
        if event["type"] == "closed":
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
