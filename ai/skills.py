"""Skills: knowledge the model loads when a task needs it, instead of carrying it in every request.

A skill is a folder holding SKILL.md: a short header saying what it is and when it helps, then the body.

    ---
    name: data-model
    description: Tables and how they link. Load before reading tables with cc_related or SQL.
    ---
    <body: facts, formats, examples - knowledge, not steps the model must follow>

Three levels of disclosure:
  1. index   one line per skill (name + description) in the system prompt: ~15 tokens each, every request
  2. body    load_skill(name) returns it as a tool result, once, when the model decides it needs it
  3. files   anything else in the folder, which the body can point to (load_skill(name, file))

Core skills live in ai/skills/<name>/, an app's in ai/apps/<app>/skills/<name>/ (an app skill replaces a core
one of the same name). A body may hold {{PLACEHOLDERS}} for generated content; the app's `fill` replaces them.

Tool search (TOOL_SEARCH=true) is the same idea for tool definitions: tools marked `defer` are sent with
defer_loading, hidden from the model until it searches for them, and `tool_search_tool()` adds the search.
"""

import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import BaseModel, Field

from ai.context import TurnContext
from ai.tools import Tool, tool

CORE_DIR = Path(__file__).parent / 'skills'
BODY_MAX_CHARS = 12_000  # ~3k tokens: a skill bigger than this belongs in files the body points to

Fill = Callable[[str, TurnContext], Awaitable[str]]


class Skill(BaseModel):
    name: str = Field(pattern=r'^[a-z0-9][a-z0-9-]*$')
    description: str = Field(min_length=10, max_length=300)
    body: str
    folder: Path


def _parse(path: Path) -> Skill:
    text = path.read_text(encoding='utf-8')
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)$', text, re.DOTALL)
    if not match:
        raise ValueError(f'{path}: SKILL.md must start with a --- header ---')
    header = dict(line.split(':', 1) for line in match.group(1).splitlines() if ':' in line)
    return Skill(name=header.get('name', '').strip(), description=header.get('description', '').strip(),
                 body=match.group(2).strip(), folder=path.parent)


def discover(*dirs: Path) -> dict[str, Skill]:
    """Skills in these folders, later folders replacing earlier ones by name."""
    found: dict[str, Skill] = {}
    for d in dirs:
        for path in sorted(d.glob('*/SKILL.md')) if d.is_dir() else []:
            skill = _parse(path)
            found[skill.name] = skill
    return found


def index(skills: dict[str, Skill]) -> str:
    """The system prompt's part: what each skill is for, one line each."""
    if not skills:
        return ''
    lines = '\n'.join(f'- `{s.name}`: {s.description}' for s in skills.values())
    return ('# Skills\n\nKnowledge to load with `load_skill` when the task needs it (it is not in this prompt):\n'
            f'{lines}')


def load_skill_tool(skills: dict[str, Skill], fill: Fill | None = None) -> Tool:
    async def load_skill(ctx: TurnContext, name: str, file: str = '') -> str:
        """
        Load a skill listed under Skills in the system prompt: its full text, or one of its files.

        :param name: the skill's name
        :param file: a file the skill's text points to; empty for the skill itself
        :return: the skill's text
        """
        skill = skills.get(name)
        if not skill:
            return f'No skill {name!r}. Skills: {", ".join(skills)}'
        if file:
            path = (skill.folder / file).resolve()
            if skill.folder.resolve() not in path.parents or not path.is_file():
                files = [p.name for p in skill.folder.iterdir() if p.name != 'SKILL.md']
                return f'No file {file!r} in skill {name}. Files: {", ".join(files) or "none"}'
            return path.read_text(encoding='utf-8')
        return await fill(skill.body, ctx) if fill else skill.body

    return tool(load_skill)


def tool_search_tool() -> Tool:
    """The provider-hosted tool search (Responses API): finds and loads tools sent with defer_loading."""
    async def never_called(ctx: TurnContext) -> str:  # the provider runs the search, not us
        return 'tool_search runs at the provider.'

    return Tool(name='tool_search', description='', parameters={}, fn=never_called, raw_spec={'type': 'tool_search'})
