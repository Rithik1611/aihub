"""cc_code (Code Mode) against a fake cableCORE_BE: pause -> approval card -> approve / decline; modes."""

import asyncio
import json
from typing import Any

import pytest

from ai.apps.cablecore import codemode, records
from ai.apps.cablecore.tools import tools as cablecore_tools
from ai.context import Card, TurnContext
from ai.settings import settings

PENDING = {'executionId': 'e1', 'seq': 1, 'connector': 'cablecore', 'method': 'design_copy', 'args': {'body': {'code': 'LV-1'}}}


def fake_be(monkeypatch: pytest.MonkeyPatch, sent: list[tuple[str, dict[str, Any]]], execute: dict[str, Any]) -> None:
    async def call(ctx: TurnContext, op: str, body: dict[str, Any]) -> dict[str, Any]:
        sent.append((op, body))
        if op == 'execute':
            return execute
        if op == 'describe':
            return {'description': 'Copy a design | POST /design/copy | design copy | fields: code'}
        if op == 'approve':
            return {'status': 'completed', 'executionId': 'e1', 'result': {'copied': 'LV-2'},
                    'calls': [{'seq': 0, 'connector': 'cablecore', 'method': 'design_search', 'state': 'applied'},
                              {**PENDING, 'state': 'applied', 'result': {'code': 'LV-2'}}]}
        if op == 'reject':
            return {'rejected': True}
        raise AssertionError(op)

    async def find_any(_ref: str) -> None:
        return None

    monkeypatch.setattr(codemode, '_call', call)
    monkeypatch.setattr(records, 'find_any', find_any)


def ctx(approve: bool, cards: list[Card]) -> TurnContext:
    async def wait_answer(_card_id: str, card: Card) -> dict[str, Any]:
        cards.append(card)
        return {'approved': approve}

    return TurnContext(user_id='u1', user_email='u@x', cc_token='jwt', chat_id='c1', wait_answer=wait_answer)


def test_paused_change_is_approved_with_our_card(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []
    fake_be(monkeypatch, sent, {'status': 'paused', 'executionId': 'e1', 'pending': [PENDING], 'calls': []})
    cards: list[Card] = []
    c = ctx(True, cards)
    out = json.loads(asyncio.run(codemode.cc_code(c, 'async () => 1')))
    assert out['status'] == 'completed' and out['result'] == {'copied': 'LV-2'}
    item = cards[0].items[0]  # type: ignore[union-attr]
    assert (item.summary, item.method, item.path) == ('Copy a design', 'POST', '/design/copy') and 'LV-1' in item.args
    assert [op for op, _ in sent] == ['execute', 'describe', 'approve']
    assert [w.action for w in c.writes] == ['design_copy'] and c.writes[0].status == 'done'  # only the change, not the read


def test_declined_change_ends_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []
    fake_be(monkeypatch, sent, {'status': 'paused', 'executionId': 'e1', 'pending': [PENDING],
                                'calls': [{'method': 'design_search', 'state': 'applied'}]})
    c = ctx(False, [])
    out = json.loads(asyncio.run(codemode.cc_code(c, 'async () => 1')))
    assert out['status'] == 'declined' and out['not_run'] == ['design_copy'] and out['ran_before'] == ['design_search']
    assert ('reject', {'chatId': 'c1', 'executionId': 'e1', 'seq': 1}) in sent and not c.writes


def test_code_error_comes_back_to_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_be(monkeypatch, [], {'status': 'error', 'executionId': 'e2', 'error': 'cablecore.nope is not a function', 'logs': ['x']})
    out = json.loads(asyncio.run(codemode.cc_code(ctx(True, []), 'async () => cablecore.nope()')))
    assert out == {'status': 'error', 'result': None, 'error': 'cablecore.nope is not a function', 'logs': ['x'], 'calls': []}


@pytest.mark.parametrize('mode, expected', [
    ('harness', ['cc_search', 'cc_get', 'cc_related', 'cc_formula', 'cc_find', 'cc_describe', 'cc_action', 'cc_note', 'cablecore_api_access']),
    ('codemode', ['cc_search', 'cc_get', 'cc_related', 'cc_formula', 'cc_code', 'cc_note', 'cablecore_api_access']),
    ('codemode_all', ['cc_code', 'cc_note', 'cablecore_api_access']),
])
def test_tool_modes(monkeypatch: pytest.MonkeyPatch, mode: str, expected: list[str]) -> None:
    monkeypatch.setattr(settings(), 'cc_tool_mode', mode)
    got = cablecore_tools()
    assert [t.name for t in got] == expected
    code_tool = next((t for t in got if t.name == 'cc_code'), None)
    if code_tool:
        assert code_tool.sequential and '{dc}' not in code_tool.description
        assert ('`dc`' in code_tool.description) == (mode == 'codemode_all')
