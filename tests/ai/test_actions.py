"""Write harness without a backend: a tiny OpenAPI spec, a fake cableCORE API and a fake user."""

import asyncio
import json

from ai.apps.cablecore import actions, api, index, records
from ai.context import TurnContext

SPEC = {
    'paths': {
        '/design/{id}/copy': {'post': {'operationId': 'design.copy', 'description': 'Copy a design. Risk: write',
                                       'parameters': [{'name': 'id', 'in': 'path'}]}},
        '/design/{id}/attributes': {'patch': {'operationId': 'design.update_plant_attributes', 'description': 'Update header values.',
                                              'parameters': [{'name': 'id', 'in': 'path'}],
                                              'requestBody': {'content': {'application/json': {'schema': {
                                                  'type': 'object', 'required': ['values'], 'properties': {'values': {'type': 'object'}}}}}}}},
        '/os/designs/search': {'post': {'operationId': 'search_designs', 'description': 'Search designs. Risk: read'}},
        '/design/{id}': {'get': {'operationId': 'get_design', 'parameters': [{'name': 'id', 'in': 'path'}]}},
    }
}


def setup(monkeypatch, calls):
    async def spec(_token):
        return SPEC

    async def caps(_token):
        return {}

    async def call(method, path, token, **kw):
        calls.append((method, path, kw.get('body')))
        return 200, {'statusCode': 200, 'data': {'id': 'NEW-1'}}

    async def find_any(_ref):
        return None

    monkeypatch.setattr(api, 'spec', spec)
    monkeypatch.setattr(api, 'capabilities', caps)
    monkeypatch.setattr(api, 'call', call)
    monkeypatch.setattr(records, 'find_any', find_any)
    monkeypatch.setattr(index, '_bm25', None)


def ctx(answer, cards):
    async def wait_answer(_card_id, _card):
        return answer

    return TurnContext(user_id='u1', user_email='u@x', cc_token='jwt', chat_id='c1', wait_answer=wait_answer,
                       emit=lambda event: cards.append(event['card']))


def test_invalid_route_runs_nothing(monkeypatch):
    calls, cards = [], []
    setup(monkeypatch, calls)
    out = json.loads(asyncio.run(actions.run([{'id': 'design.nope'}], None, ctx({'approved': True}, cards))))
    assert out['status'] == 'invalid' and out['nothing_ran'] and not cards and not calls


def test_schema_problem_runs_nothing(monkeypatch):
    calls, cards = [], []
    setup(monkeypatch, calls)
    act = {'id': 'design.update_plant_attributes', 'path': {'id': 'D1'}, 'body': {}}
    out = json.loads(asyncio.run(actions.run([act], None, ctx({'approved': True}, cards))))
    assert out['status'] == 'invalid' and 'values' in out['problems'][0] and not cards and not calls


def test_declined_runs_nothing(monkeypatch):
    calls, cards = [], []
    setup(monkeypatch, calls)
    c = ctx({'approved': False}, cards)
    out = json.loads(asyncio.run(actions.run([{'id': 'design.copy', 'path': {'id': 'D1'}}], None, c)))
    assert out['status'] == 'declined' and not calls and not c.writes
    assert cards[0]['kind'] == 'approval' and cards[0]['items'][0]['path'] == '/design/D1/copy'


def test_approved_batch_chains_refs_and_logs_in_turn(monkeypatch):
    calls, cards = [], []
    setup(monkeypatch, calls)
    c = ctx({'approved': True}, cards)
    batch = [
        {'id': 'design.copy', 'path': {'id': 'D1'}},
        {'id': 'design.update_plant_attributes', 'path': {'id': '$1.id'}, 'body': {'values': {'voltage': '11kV'}}},
    ]
    out = json.loads(asyncio.run(actions.run(batch, None, c)))
    assert [r['status'] for r in out['results']] == ['done', 'done']
    assert len(cards) == 1 and len(cards[0]['items']) == 2  # ONE approval for the whole batch
    assert calls == [('POST', '/design/D1/copy', None), ('PATCH', '/design/NEW-1/attributes', {'values': {'voltage': '11kV'}})]
    assert [w.action for w in c.writes] == ['design.copy', 'design.update_plant_attributes']


def test_sandbox_gateway_read_check(monkeypatch):
    setup(monkeypatch, [])
    check = lambda m, p: asyncio.run(index.is_read_route(m, p, 'jwt'))  # noqa: E731
    assert check('GET', 'design/D1')
    assert check('POST', 'os/designs/search')
    assert not check('POST', 'design/D1/copy')
    assert not check('PATCH', 'design/D1/attributes')
