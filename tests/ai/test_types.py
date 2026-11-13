"""Pydantic boundaries: tool arguments, the user's answer, the event stream; LangChain adapters; tracing switch."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import ai
from ai import apps, http, loop, sandbox
from ai.context import Answer, ApprovalCard, Card, CardEvent, Done, Items, TextDelta, TurnContext
from ai.langchain import as_langchain
from ai.settings import settings
from ai.tools import run, tool

from tests.ai.test_actions import setup as fake_cablecore


def ctx_with(answer: Any) -> TurnContext:
    async def wait_answer(_card_id: str, _card: Card) -> Any:
        return answer

    return TurnContext(user_id='u1', user_email='u@x', cc_token='jwt', chat_id='c1', wait_answer=wait_answer)


async def lookup(ctx: TurnContext, code: str, kind: str = 'design', fields: list[str] = None) -> str:  # type: ignore[assignment]
    """Look a record up.

    :param code: record code
    :param kind: design or enquiry
    :param fields: values to add
    """
    return json.dumps({'code': code, 'kind': kind, 'fields': fields})


def test_bad_arguments_never_reach_the_tool():
    t = tool(lookup)
    status, text = asyncio.run(run({'lookup': t}, ctx_with({}), 'lookup', '{"kind": "design"}'))
    assert status == 'failed' and 'code' in json.loads(text)['problems'][0]
    status, text = asyncio.run(run({'lookup': t}, ctx_with({}), 'lookup', '{"code": "LV-1", "fields": null, "extra": 1}'))
    assert status == 'completed' and json.loads(text) == {'code': 'LV-1', 'kind': 'design', 'fields': None}  # null ok, unknown keys dropped
    assert t.parameters['required'] == ['code'] and 'title' not in json.dumps(t.parameters)


def test_malformed_approval_runs_nothing(monkeypatch):
    calls: list[Any] = []
    fake_cablecore(monkeypatch, calls)
    from ai.apps.cablecore.tools import cc_action

    t = tool(cc_action, sequential=True)
    args = json.dumps({'actions': [{'id': 'design.copy', 'path': {'id': 'D1'}}]})
    for answer in ({'approved': 'yes'}, {'approved': 1}, {'approved': True, 'admin': True}):
        status, text = asyncio.run(run({'cc_action': t}, ctx_with(answer), 'cc_action', args))
        assert status == 'failed' and 'ValidationError' in text, answer
    assert calls == []  # the copy never ran
    with pytest.raises(ValidationError):
        Answer.model_validate({'approved': 'true'})


def test_run_turn_yields_validated_models(monkeypatch):
    async def instructions(ctx: TurnContext) -> str:
        return 'You help.'

    async def after_turn(ctx: TurnContext, request: str, reply: str) -> list[Any]:
        return []

    async def create(**kw: Any) -> Any:
        r = SimpleNamespace(output=[], usage=None)
        msg = SimpleNamespace(model_dump=lambda exclude_none=True: {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'hi'}]})
        r.output = [msg]

        async def stream():
            yield SimpleNamespace(type='response.output_text.delta', delta='hi')
            yield SimpleNamespace(type='response.completed', response=r)
        return stream()

    async def no_tools(ctx: TurnContext) -> list[Any]:
        return []

    async def no_prompt(ctx: TurnContext) -> str:
        return ''

    monkeypatch.setattr(http, 'llm', lambda: SimpleNamespace(responses=SimpleNamespace(create=create)))
    monkeypatch.setattr(apps, 'get', lambda _id: SimpleNamespace(instructions=instructions, tools=lambda: [], after_turn=after_turn))
    monkeypatch.setattr(sandbox, 'tools', no_tools)
    monkeypatch.setattr(sandbox, 'instructions', no_prompt)

    async def collect() -> list[Any]:
        return [e async for e in ai.run_turn(ctx_with({}), [], ai.user_message('hello'))]

    events = asyncio.run(collect())
    assert [type(e) for e in events] == [TextDelta, Items, Done]
    assert json.loads(events[0].model_dump_json()) == {'type': 'text.delta', 'text': 'hi'}
    assert loop.run_turn is not ai.run_turn  # the loop's own generator is untouched; ai.run_turn wraps it


def test_card_event_carries_a_typed_card():
    sent: list[Any] = []
    ctx = ctx_with({'approved': True})
    ctx.emit = sent.append
    card = ApprovalCard(title='Approve 1 change', items=[], markdown='')
    assert asyncio.run(ctx.ask(card)).approved is True
    assert isinstance(CardEvent.model_validate(sent[0]).card, ApprovalCard)


def test_langchain_tool_goes_through_validation():
    lc = as_langchain(tool(lookup), ctx_with({}))
    assert lc.name == 'lookup' and set(lc.args) == {'code', 'kind', 'fields'}
    out = asyncio.run(lc.ainvoke({'code': 'LV-1'}))
    assert json.loads(out)['code'] == 'LV-1'
    with pytest.raises(ValidationError):
        asyncio.run(lc.ainvoke({'kind': 'design'}))  # LangChain validates with the same Pydantic model


def test_tracing_wraps_the_model_client_only_when_enabled(monkeypatch):
    monkeypatch.setattr(http, '_llm', None)
    monkeypatch.setattr(settings(), 'langsmith_tracing', False)
    plain = http.llm()
    monkeypatch.setattr(http, '_llm', None)
    monkeypatch.setattr(settings(), 'langsmith_tracing', True)
    traced = http.llm()
    monkeypatch.setattr(http, '_llm', None)
    assert type(plain).__name__ == 'AsyncOpenAI' and traced is not plain
    assert hasattr(traced.responses, 'create')
