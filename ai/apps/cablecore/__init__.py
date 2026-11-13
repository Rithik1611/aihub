"""cableCORE DesignCenter app pack. What the loop takes from an app: instructions, tools, after_turn.

api      HTTP calls to cableCORE with the user's own JWT; the OpenAPI document and AI capabilities.
index    every route (1.5k) searchable by intent; schemas and risk per route; read-only check for the sandbox gateway.
records  read-back of designs and enquiries from the read-only database.
related  rows of any Prisma model, linked through the schema's foreign keys.
formula  an attribute's formula, resolved like the app, with dependencies and lookup tables.
actions  validate -> approve (ctx.ask) -> run -> follow job -> read back; changes logged in ctx.writes.
notes    facts verified in chats, added to every later chat.
tools    the model's cableCORE tools.  prompt  the system prompt.  hooks  the after-turn checks.
"""

from ai.apps.cablecore.hooks import after_turn
from ai.apps.cablecore.prompt import instructions
from ai.apps.cablecore.tools import tools

__all__ = ['after_turn', 'instructions', 'tools']
