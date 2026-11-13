"""Bounds on AI-supplied message metadata.

A malformed or oversized AI response must never bloat the JSONB column, so every
field is clamped before it is stored. Ported from cableCORE's clampChatMetadata.
"""

import json
from typing import Final

from app.chat.schemas import ChatMessageMetadata, ChatQuestion, ChatTask, TaskDetail

MAX_TASKS: Final = 50
MAX_TASK_DETAILS: Final = 20
MAX_QUESTIONS: Final = 10
MAX_OPTIONS: Final = 20
MAX_TEXT: Final = 500
MAX_QUESTION_LEAD: Final = 4000
MAX_SUGGESTIONS: Final = 10
MAX_STATE_BYTES: Final = 32_768


def _clamp_text(value: str, limit: int = MAX_TEXT) -> str:
    return value[:limit]


def _clamp_task(task: ChatTask) -> ChatTask:
    return ChatTask(
        key=_clamp_text(task.key),
        label=_clamp_text(task.label),
        amount=_clamp_text(task.amount),
        status=task.status,
        details=[
            TaskDetail(label=_clamp_text(d.label), meta=_clamp_text(d.meta)) for d in task.details[:MAX_TASK_DETAILS]
        ],
    )


def _clamp_question(question: ChatQuestion) -> ChatQuestion:
    return ChatQuestion(
        q=_clamp_text(question.q),
        type=question.type,
        options=[_clamp_text(o) for o in question.options[:MAX_OPTIONS]],
        descriptions=[_clamp_text(d) for d in question.descriptions[:MAX_OPTIONS]],
    )


def _state_fits(state: dict[str, object]) -> bool:
    try:
        return len(json.dumps(state)) <= MAX_STATE_BYTES
    except (TypeError, ValueError):
        # Not serialisable means it cannot be stored as JSONB either.
        return False


def clamp_metadata(metadata: ChatMessageMetadata) -> ChatMessageMetadata:
    """Return a copy with every field bounded. Oversized state is dropped, not truncated."""
    return ChatMessageMetadata(
        state=(metadata.state if metadata.state is not None and _state_fits(metadata.state) else None),
        suggestions=(
            [_clamp_text(s) for s in metadata.suggestions[:MAX_SUGGESTIONS]]
            if metadata.suggestions is not None
            else None
        ),
        attachment=metadata.attachment,
        tasks=([_clamp_task(t) for t in metadata.tasks[:MAX_TASKS]] if metadata.tasks is not None else None),
        questions=(
            [_clamp_question(q) for q in metadata.questions[:MAX_QUESTIONS]] if metadata.questions is not None else None
        ),
        question_lead=(
            _clamp_text(metadata.question_lead, MAX_QUESTION_LEAD) if metadata.question_lead is not None else None
        ),
        stopped=metadata.stopped if metadata.stopped else None,
        complete=metadata.complete if metadata.complete else None,
    )
