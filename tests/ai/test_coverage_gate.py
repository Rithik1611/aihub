"""The completeness check after a turn runs only for answers about the user's DesignCenter data."""

import asyncio

from ai.apps.cablecore import hooks
from ai.context import TurnContext


def _ctx() -> TurnContext:
    return TurnContext(user_id='u', user_email='u@x', cc_token='t', chat_id='c', wait_answer=None)  # type: ignore[arg-type]


def test_general_knowledge_answers_are_not_checked(monkeypatch):
    async def must_not_run(request, reply, user_id):
        raise AssertionError('checked a general answer')

    monkeypatch.setattr(hooks, '_coverage_check', must_not_run)
    assert asyncio.run(hooks.coverage(_ctx(), 'LiHH vs LiHCH?', 'LiHCH has a braid screen.', ['search_web', 'fetch_url'])) is None
    assert asyncio.run(hooks.coverage(_ctx(), 'LiHH vs LiHCH?', 'LiHCH has a braid screen.', [])) is None


def test_answers_about_designcenter_data_are_checked(monkeypatch):
    async def found(request, reply, user_id):
        return {'type': 'check', 'kind': 'coverage', 'note': 'gap'}

    monkeypatch.setattr(hooks, '_coverage_check', found)
    assert asyncio.run(hooks.coverage(_ctx(), 'insulation of LV-1?', '0.7 mm', ['cc_get']))['note'] == 'gap'
