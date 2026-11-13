"""ask_user: a question card; the turn continues with the user's answer."""

import json
from typing import Annotated

from pydantic import Field

from ai.context import Question, QuestionCard, TurnContext
from ai.tools import tool


async def ask_user(ctx: TurnContext, questions: Annotated[list[Question], Field(min_length=1, max_length=3)],
                   allow_other: bool = True, timeout_ms: int = 120_000) -> str:
    """
    Ask the user clarifying questions before continuing.
    Use this when the next step depends on user intent, preference, or a tradeoff that cannot be inferred safely.

    :param questions: 1-3 question objects, each with id, header, question, and 2-3 options. Each option needs label and description.
        List the option you recommend first; the UI labels the first option Recommended.
    :param allow_other: Whether users may enter a free-form answer instead of choosing one of the options
    :param timeout_ms: How long the browser should keep the prompt open before cancelling it
    :return: JSON with status and answers keyed by question id
    """
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        return json.dumps({'status': 'error', 'error': 'Each question needs a unique id.'})
    card = QuestionCard(
        questions=[q.model_copy(update={'header': q.header or f'Question {i + 1}', 'allow_other': q.allow_other if 'allow_other' in q.model_fields_set else allow_other})
                   for i, q in enumerate(questions)],
        timeout_ms=timeout_ms if 60_000 <= timeout_ms <= 240_000 else 120_000,
    )
    answer = await ctx.ask(card)
    if answer.status != 'answered':
        return json.dumps({'status': answer.status or 'cancelled', 'answers': {}}, ensure_ascii=False)
    return json.dumps({'status': 'answered', 'answers': answer.answers}, ensure_ascii=False)


TOOL = tool(ask_user, sequential=True)
