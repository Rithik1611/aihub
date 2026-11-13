import asyncio
import re

import pytest

from ai import skills
from ai.apps.cablecore.prompt import SKILLS
from ai.settings import settings
from ai.tools import tool

KNOWN_PLACEHOLDERS = {'{{CATALOGUE}}', '{{DATA_MAP}}', '{{SCHEMA_INDEX}}', '{{USER_EMAIL}}', '{{CURRENT_DATE}}'}


def test_every_skill_parses_within_budget_and_uses_known_placeholders():
    assert SKILLS, 'no skills found'
    for s in SKILLS.values():
        assert len(s.body) <= skills.BODY_MAX_CHARS, f'{s.name}: body too long'
        assert set(re.findall(r'\{\{\w+\}\}', s.body)) <= KNOWN_PLACEHOLDERS, s.name
    assert 'chat-output' in SKILLS  # core skills are found too


def test_load_skill_reads_only_inside_the_skill(tmp_path):
    (tmp_path / 'demo').mkdir()
    (tmp_path / 'demo' / 'SKILL.md').write_text('---\nname: demo\ndescription: A demo skill for tests.\n---\nBody', encoding='utf-8')
    (tmp_path / 'demo' / 'ref.md').write_text('reference', encoding='utf-8')
    (tmp_path / 'secret.txt').write_text('secret', encoding='utf-8')
    load = skills.load_skill_tool(skills.discover(tmp_path)).fn
    assert asyncio.run(load(None, 'demo')) == 'Body'
    assert asyncio.run(load(None, 'demo', 'ref.md')) == 'reference'
    assert 'No file' in asyncio.run(load(None, 'demo', '../secret.txt'))
    assert 'No skill' in asyncio.run(load(None, 'nope'))


def test_bad_header_is_rejected(tmp_path):
    (tmp_path / 'x').mkdir()
    (tmp_path / 'x' / 'SKILL.md').write_text('no header', encoding='utf-8')
    with pytest.raises(ValueError):
        skills.discover(tmp_path)


def test_deferred_tools_are_marked_only_with_tool_search(monkeypatch):
    async def demo(ctx) -> str:
        """A demo tool."""
        return ''

    t = tool(demo, defer=True)
    monkeypatch.setattr(settings(), 'tool_search', False)
    assert 'defer_loading' not in t.spec()
    monkeypatch.setattr(settings(), 'tool_search', True)
    assert t.spec()['defer_loading'] is True
    assert skills.tool_search_tool().spec() == {'type': 'tool_search'}


def test_zero_match_hints_come_from_the_search_index(monkeypatch):
    from ai.apps.cablecore import attributes
    from ai.apps.cablecore.tools import _name_hint

    async def keys(token):
        return frozenset({'conductortype', 'noofconductors', 'layup.noelements', 'layup_conc.noelements'})

    async def meta():
        return {'conductortype': {'key': 'conductorType'}}

    async def values(key):
        return 10, [('CU', 8), ('AL', 2)]

    monkeypatch.setattr(attributes, 'index_keys', keys)
    monkeypatch.setattr(attributes, 'meta', meta)
    monkeypatch.setattr(attributes, 'values', values)
    def hint(f):
        return asyncio.run(_name_hint(f, 'token'))

    assert 'not a key in the search index' in hint({'field': 'noOfConductor', 'type': 'multi-path'})
    assert 'noofconductors' in hint({'field': 'noOfConductor', 'type': 'multi-path'})
    assert 'layup, layup_conc' in hint({'field': 'noElements', 'processName': 'STRANDING', 'type': 'process-attribute'})
    assert 'CU (8)' in hint({'field': 'conductorType', 'type': 'multi-path'})  # right key, wrong value: show real values
    assert hint({'field': 'designId', 'type': 'top-level'}) == ''


def test_cc_search_refuses_free_text(monkeypatch):
    import json

    from ai.apps.cablecore import api
    from ai.apps.cablecore.tools import cc_search
    from ai.context import TurnContext

    async def fake_call(method, path, token, body=None, **kw):
        terms = [w for w in body['query'].split() if '=' not in w]
        return 201, {'data': [{'designId': 'LV-1'}], 'pagination': {'total': 4931},
                     'parsedQuery': {'filters': [], 'textTerms': terms, 'orGroups': []}}

    monkeypatch.setattr(api, 'call', fake_call)
    ctx = TurnContext(user_id='u', user_email='u@x', cc_token='t', chat_id='c', wait_answer=None)  # type: ignore[arg-type]
    out = json.loads(asyncio.run(cc_search(ctx, 'design', 'PVC conductorType=CU')))
    assert 'refused' in out and 'PVC' in out['refused'] and 'rows' not in out
    assert 'rows' in json.loads(asyncio.run(cc_search(ctx, 'design', 'conductorType=CU')))
