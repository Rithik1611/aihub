"""After a turn: a reply that changed data is checked against what cc_action did and what the records
now say; a reply that changed nothing is checked for covering the whole request. Findings come back as
notes shown under the reply. A check never breaks a reply."""

import asyncio
import logging
import re
from typing import Any

from pydantic import BaseModel

from ai import sandbox
from ai.context import Event, TurnContext, WriteRecord
from ai.langchain import structured

log = logging.getLogger(__name__)

CHECK_PROMPT = """You check an assistant's reply against the changes it made in cableCORE in this turn.

The user asked:
{request}

Changes run (action, route, request, status, response), in order:
{writes}

The affected records as stored after the changes:
{after}

The assistant's reply:
{reply}

Judge only statements that THIS turn changed something (created, copied, updated, set, assigned, attached, imported,
submitted, deleted). Facts the reply reports about existing data, plans, questions, and things the reply itself says
were not done are out of scope. A failed action followed by a successful one doing the same thing counts as done.
Values: a record's process values hold what a process was set to (e.g. SHEATHING colour); descriptions and other text
the app derives by formula may lag behind until the design is recalculated, so they do not contradict a set value.

List:
- "contradicted": change statements the data above shows to be false: a different value is stored, the value is
  empty/null in the records after the change although the reply says it was set, the action failed and was not
  redone, or the record does not exist. Quote the statement and say what the data shows.
- "unverified": change statements the data above neither confirms nor contradicts (e.g. records a background job
  created that were not read back). Quote the statement briefly.
Leave a list empty when nothing applies. Never put a statement in "contradicted" unless the data shows otherwise.
Answer with JSON only: {{"contradicted": ["..."], "unverified": ["..."]}}"""

COVERAGE_PROMPT = """You check whether an assistant's answer covers what the user asked. Judge completeness only, not facts.

The user asked:
{request}
{attachment}
The assistant's answer:
{reply}

List:
- "missed": parts of the request the answer leaves out without saying why: a question not answered, a record asked about
  but not covered, rows or columns of the user's data not addressed. Name each exactly (e.g. "row 3 x 150, column
  outer sheath thickness"). Things the answer says it could not do, with a reason, are not missed.
- "unsupported": places where the answer says something matches, is covered by, complies with or reproduces the user's
  data or a standard, but shows no value, lookup row or formula behind it. Restated record details are not this. Quote briefly.
Only list gaps a careful reviewer would raise; at most 5 per list, the most important first. Leave a list empty when
nothing applies; most answers have both empty.
Answer with JSON only: {{"missed": ["..."], "unsupported": ["..."]}}"""

ATTACHED = re.compile(r'\[Attached file "[^"]*" is at ([^\]]+)\]')


class WritesVerdict(BaseModel):
    contradicted: list[str]  # change statements the data shows to be false
    unverified: list[str]  # change statements the data neither confirms nor contradicts


class CoverageVerdict(BaseModel):
    missed: list[str]  # parts of the request left out without a reason
    unsupported: list[str]  # "matches / complies" claims with no value, row or formula shown


def _unresolved(writes: list[WriteRecord]) -> list[WriteRecord]:
    """Failed changes that no later successful run of the same action made good."""
    return [w for i, w in enumerate(writes)
            if w.status != 'done' and not any(later.action == w.action and later.status == 'done' for later in writes[i + 1:])]


async def _writes_check(request: str, reply: str, writes: list[WriteRecord]) -> Event | None:
    findings = [f'`{w.action}` {w.status}: {w.response[:200]}' for w in _unresolved(writes)]
    if any(w.status == 'done' and w.risk != 'read' for w in writes):
        ledger = '\n'.join(
            f'{i + 1}. {w.action}: {w.method} {w.path} -> {w.status}\n   request: {w.request}\n   response: {w.response[:1500]}'
            for i, w in enumerate(writes))
        after = '\n'.join(w.after for w in writes if w.after) or '(no records were read back)'
        try:
            verdict = await structured(WritesVerdict).ainvoke(
                CHECK_PROMPT.format(request=request[:4000], writes=ledger[:16000], after=after[:30000], reply=reply[:8000]))
            findings += [f'⚠️ Contradicted by the data: {s}' for s in verdict.contradicted]
            findings += [f'Not verified after the change (check in DesignCenter): {s}' for s in verdict.unverified]
        except Exception as e:
            findings.append(f'The automatic check could not run ({type(e).__name__}); verify the changes in DesignCenter.')
    if not findings:
        return None
    note = '\n\n---\n**Checked against the changes actually made in cableCORE:**\n' + '\n'.join(f'- {f}' for f in findings)
    return {'type': 'check', 'kind': 'writes', 'note': note}


async def _coverage_check(request: str, reply: str) -> Event | None:
    try:
        attachment = ''
        if found := ATTACHED.search(request):
            try:
                _, content, _ = await sandbox.read_file(found.group(1).strip())
                attachment = f'\nThe attached file ({found.group(1).strip()}):\n{content.decode("utf-8", "replace")[:12000]}\n'
            except Exception:  # an unreadable (e.g. binary) file: judge from the request alone
                pass
        verdict = await structured(CoverageVerdict).ainvoke(
            COVERAGE_PROMPT.format(request=request[:6000], attachment=attachment, reply=reply[:16000]))
    except Exception as e:
        log.warning(f'completeness check failed: {type(e).__name__}: {e}')
        return None
    lines = [f'- Not covered: {s}' for s in verdict.missed]
    lines += [f'- Stated without the evidence shown: {s}' for s in verdict.unsupported]
    if not lines:
        return None
    return {'type': 'check', 'kind': 'coverage', 'note': '\n\n---\n**Automatic completeness check:**\n' + '\n'.join(lines)}


async def after_turn(ctx: TurnContext, request: str, reply: str) -> list[Event]:
    """Check events, as wire dicts like everything the loop yields."""
    if not reply:
        return []
    changed = any(w.status == 'done' and w.risk != 'read' for w in ctx.writes)
    checks: list[Any] = []
    if ctx.writes:
        checks.append(_writes_check(request, reply, ctx.writes))
    if not changed:
        checks.append(_coverage_check(request, reply))
    return [c for c in await asyncio.gather(*checks) if c]
