"""The loop with a fake model: tool calls, a card answered by the 'user', streamed text, history items."""

import asyncio
import json
from types import SimpleNamespace

from ai import apps, ask_user, http, loop, sandbox
from ai.context import TurnContext, user_message
from ai.tools import tool


class Out(SimpleNamespace):
    def model_dump(self, exclude_none=True):
        return {k: v for k, v in vars(self).items() if v is not None}


def response(*output):
    return SimpleNamespace(output=list(output), usage=SimpleNamespace(input_tokens=10, output_tokens=5))


class FakeModel:
    """Step 1: asks the user and looks something up in parallel. Step 2: answers."""

    def __init__(self):
        self.requests = []
        self.responses = SimpleNamespace(create=self.create)

    async def create(self, **kw):
        self.requests.append(kw)
        if len(self.requests) == 1:
            r = response(
                Out(type='reasoning', id='rs_1', encrypted_content='enc', status='completed'),
                Out(type='function_call', call_id='c1', name='ask_user', arguments=json.dumps({'questions': [
                    {'id': 'plant', 'question': 'Which plant?', 'options': [{'label': 'A', 'description': 'plant A'},
                                                                            {'label': 'B', 'description': 'plant B'}]}]})),
                Out(type='function_call', call_id='c2', name='lookup', arguments='{"code": "LV-1"}'),
            )
            events = []
        else:
            r = response(Out(type='message', role='assistant', content=[{'type': 'output_text', 'text': 'Plant B: 3 cores.'}]))
            events = [SimpleNamespace(type='response.output_text.delta', delta='Plant B: '),
                      SimpleNamespace(type='response.output_text.delta', delta='3 cores.')]

        async def stream():
            for e in events:
                yield e
            yield SimpleNamespace(type='response.completed', response=r)

        return stream()


def test_turn(monkeypatch):
    async def lookup(ctx: TurnContext, code: str) -> str:
        """Look a design up.

        :param code: design code
        """
        return json.dumps({'code': code, 'cores': 3})

    async def instructions(ctx):
        return 'You help.'

    async def after_turn(ctx, request, reply):
        return [{'type': 'check', 'kind': 'coverage', 'note': 'covered'}]

    async def no_tools(ctx):
        return []

    async def no_prompt(ctx):
        return ''

    fake = FakeModel()
    monkeypatch.setattr(http, 'llm', lambda: fake)
    monkeypatch.setattr(apps, 'get', lambda _id: SimpleNamespace(instructions=instructions, tools=lambda: [tool(lookup)], after_turn=after_turn))
    monkeypatch.setattr(sandbox, 'tools', no_tools)
    monkeypatch.setattr(sandbox, 'instructions', no_prompt)

    async def wait_answer(card_id, card):
        assert card.kind == "question"  # the card arrives with the wait, not only as an event
        return {'status': 'answered', 'answers': {'plant': 'B'}}

    ctx = TurnContext(user_id='u1', user_email='u@x', cc_token='jwt', chat_id='c1', wait_answer=wait_answer)
    history = [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'earlier'}]}]

    async def collect():
        return [e async for e in loop.run_turn(ctx, history, user_message('how many cores?', screen='/design/1'))]

    events = asyncio.run(collect())
    kinds = [e['type'] for e in events]
    assert 'error' not in kinds, events
    assert kinds[-2:] == ['items', 'done']
    assert [e['card']['kind'] for e in events if e['type'] == 'card'] == ['question']
    assert {e['name'] for e in events if e['type'] == 'tool.end'} == {'ask_user', 'lookup'}
    assert ''.join(e['text'] for e in events if e['type'] == 'text.delta') == 'Plant B: 3 cores.'
    assert [e['note'] for e in events if e['type'] == 'check'] == ['covered']

    items = next(e for e in events if e['type'] == 'items')['items']
    assert '[DesignCenter screen: /design/1]' in items[0]['content'][0]['text']
    outputs = {i['call_id']: i['output'] for i in items if i['type'] == 'function_call_output'}
    assert json.loads(outputs['c1']) == {'status': 'answered', 'answers': {'plant': 'B'}}
    assert json.loads(outputs['c2'])['cores'] == 3
    assert items[-1]['content'][0]['text'] == 'covered'  # the check note is kept for the next turn

    second = fake.requests[1]
    assert second['store'] is False and second['include'] == ['reasoning.encrypted_content']
    replayed = [i for i in second['input'] if i['type'] == 'reasoning'][0]
    assert 'status' not in replayed and replayed['encrypted_content'] == 'enc'  # Azure rejects status on reasoning input
    assert second['input'][0]['content'][0]['text'] == 'earlier'
    assert {t['name'] for t in second['tools']} >= {'lookup', 'ask_user', 'search_web', 'fetch_url', 'delegate_task'}
    assert ask_user.TOOL.sequential
