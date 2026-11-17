"""MES (Mendix) app pack. For now only a guest of other apps (a DesignCenter chat that needs plant data):

guest(ctx)  a short brief for the system prompt, the user's MES read tools sent deferred (found with tool search,
            not in every request), and the `mes` skill with the answering rules (loaded when needed).

client  the MCP client: per-user token, read-only filter, schema clean-up, caching. skills/  the MES skills.
"""

from dataclasses import replace
from pathlib import Path

from ai import skills
from ai.apps import Guest
from ai.apps.mes import client
from ai.context import TurnContext

SKILLS = skills.discover(Path(__file__).parent / 'skills')

# The namespace description is what the model sees of the MES tools until tool search loads them.
NAMESPACE = ('mes', ("The user's plant MES, live and read-only: sites, machines and their state, jobs and job counts, "
                     'lots, production orders, shift windows, inventory and storage, quality orders, rework, site documents '
                     'and help topics.'))

BRIEF = """## MES (plant shop floor)

The user's plant MES is connected (the `mes` tools, loaded with tool search). For shop-floor questions (what is
running, an order's or lot's status, stock, quality) load them and `load_skill('mes')` (its answering rules) in the
same step. Designs and enquiries stay with the cableCORE tools."""


async def guest(ctx: TurnContext) -> Guest | None:
    tools = await client.tools(ctx)
    if not tools:
        return None  # no MES token, or MES is down: no brief, no tools, nothing in the prompt
    return Guest('mes', BRIEF, [replace(t, guest=True, namespace=NAMESPACE) for t in tools], SKILLS)
