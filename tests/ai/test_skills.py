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
