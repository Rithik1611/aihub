"""App packs: one folder per application.

The home app is the one the chat belongs to (ctx.app_id): its instructions(ctx), tools(), after_turn(...) shape the
turn. Other apps join as guests through guest(ctx) -> Guest | None: a short brief in the prompt, their tools sent
deferred (found with tool search, so they cost nothing until needed) and their skills behind load_skill.
"""

import asyncio
import importlib
import logging
from dataclasses import dataclass
from types import ModuleType

from ai.context import TurnContext
from ai.skills import Skill
from ai.tools import Tool

# Apps that can join another app's chat; each joins only when the user has access to it (its own credential).
GUEST_APPS = ('mes',)
log = logging.getLogger('aihub.apps')


@dataclass(frozen=True)
class Guest:
    app: str
    brief: str
    tools: list[Tool]
    skills: dict[str, Skill]


def get(app_id: str) -> ModuleType:
    if not app_id.isidentifier():
        raise ValueError(f'Unknown app {app_id!r}.')
    try:
        return importlib.import_module(f'ai.apps.{app_id}')
    except ModuleNotFoundError as e:
        raise ValueError(f'Unknown app {app_id!r}.') from e


async def guests(ctx: TurnContext) -> list[Guest]:
    """The guest apps this user has, fetched together; one that fails is left out, never the turn."""
    joins = [getattr(get(a), 'guest', None) for a in GUEST_APPS if a != ctx.app_id]
    found = await asyncio.gather(*(join(ctx) for join in joins if join), return_exceptions=True)
    for failed in (f for f in found if isinstance(f, BaseException)):
        log.warning(f'a guest app could not join: {type(failed).__name__}: {failed}')
    return [g for g in found if isinstance(g, Guest)]


def briefs(found: list[Guest]) -> str:
    """The system prompt's part for guests; empty without any, so the home prompt is unchanged."""
    return '\n\n# Other apps\n\n' + '\n\n'.join(g.brief for g in found) if found else ''
