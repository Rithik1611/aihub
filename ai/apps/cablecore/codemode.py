"""cc_code: Cloudflare Code Mode as the agent's way to the API (CC_TOOL_MODE=codemode / codemode_all).

The model writes one async JavaScript function that searches cableCORE's routes (codemode.search /
describe) and calls them (cablecore.<method>(...)); cableCORE_BE runs it in a sandboxed isolate with the
user's JWT (src/ai/codemode). A route that changes data pauses the run: the user gets the same approval
card as with cc_action, and approving resumes the run (declining ends it). Approved changes are logged
in ctx.writes and the affected records read back, so the after-turn check works as with the harness.
"""

import json
import time
from dataclasses import replace
from typing import Any

import aiohttp

from ai import http
from ai.apps.cablecore import api, records
from ai.context import ApprovalCard, ApprovalItem, TurnContext, WriteRecord
from ai.settings import settings
from ai.tools import Tool, tool

RESULT_CHARS = 30_000
MAX_PAUSES = 10  # approvals in one run; a script that keeps pausing is cut off here


class CodemodeError(Exception):
    pass


def _scope() -> str:
    return 'all' if settings().cc_tool_mode == 'codemode_all' else 'api'


async def _call(ctx: TurnContext, op: str, body: dict[str, Any]) -> dict[str, Any]:
    url = f"{settings().codemode_url.rstrip('/')}/{op}"
    async with http.session().post(url, json={**body, 'scope': _scope()}, headers={'Authorization': f'Bearer {ctx.cc_token}'},
                                   timeout=aiohttp.ClientTimeout(total=600)) as r:
        text = await r.text()
        if r.status >= 400:
            raise CodemodeError(f'Code Mode {op} failed ({r.status}): {text[:500]}')
    data = api.unwrap(json.loads(text))
    if not isinstance(data, dict):
        raise CodemodeError(f'Code Mode {op}: unexpected answer {text[:200]}')
    return data


async def _approval_card(ctx: TurnContext, pending: list[dict[str, Any]]) -> ApprovalCard:
    """The paused calls as the cc_action card shows changes: what it does, METHOD path, the exact arguments."""
    items: list[ApprovalItem] = []
    for p in pending:
        target = f"{p.get('connector', 'cablecore')}.{p.get('method', '')}"
        try:
            described = str((await _call(ctx, 'describe', {'target': target})).get('description') or '')
        except CodemodeError:
            described = ''
        parts = [s.strip() for s in described.split('|')]  # "summary | METHOD /path | words | fields"
        verb, _, path = (parts[1] if len(parts) > 1 else '').partition(' ')
        items.append(ApprovalItem(summary=(parts[0] or p.get('method', ''))[:120], method=verb or '?', path=path or target,
                                  args=json.dumps(p.get('args'), ensure_ascii=False, default=str)[:600].replace('`', '')))
    markdown = '\n\n'.join(f'{i}. **{it.summary}**  \n`{it.method} {it.path}`  \n`{it.args}`' for i, it in enumerate(items, 1))
    return ApprovalCard(title=f"Approve {len(items)} change{'s' if len(items) > 1 else ''} in cableCORE", items=items, markdown=markdown)


async def _read_back(calls: list[dict[str, Any]]) -> str:
    """The records the approved changes touched, as stored now (for the after-turn check)."""
    refs = list(dict.fromkeys(records.ids_in([{'args': c.get('args'), 'result': c.get('result')} for c in calls])))[:4]
    found = {}
    for ref in refs:
        record = await records.find_any(str(ref))
        if record:
            found[f"{record['kind']} {record['code']}"] = record
    return json.dumps(found, ensure_ascii=False, default=str)[:30_000] if found else ''


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + f'… ({len(text) - limit} more chars)'


async def cc_code(ctx: TurnContext, code: str) -> str:
    """
    Run JavaScript against the cableCORE API (and its tools) in a sandbox. Write ONE async arrow function
    that does the whole task: find routes, read, loop, filter, calculate, change; return only what the
    answer needs (a compact object), not raw API payloads.

    Workflow:
      1. const matches = await codemode.search("short intent phrase");   // e.g. "copy design", "export designs"
      2. const docs = await codemode.describe(matches.results[0].path);  // the method's exact input as TypeScript
      3. const r = await cablecore.<method>({ ...pathAndQueryParams, body: {...} });
    Rules:
      - Globals: `cablecore` (every API route as a method), `codemode` (search, describe, step){dc}. No fetch, fs,
        require or Node APIs. Never guess method names: search first. Search with 2-4 words.
      - API answers are the payload ({statusCode, message, data} is unwrapped to data).
      - Calls that change data pause the run for the user's approval; write the code as if they return
        normally. Approved, the run continues; declined, it ends and the result says what did not run.
      - Code outside connector calls must be deterministic (it is replayed on resume); wrap anything else in
        `codemode.step(name, fn)`.
      - A thrown error comes back with the logs: fix the code and run it again.

    :param code: one async arrow function, e.g. async () => { ... return {...}; }
    :return: JSON: status (completed | declined | error), result, logs, the API calls made
    """
    if not ctx.cc_token:
        raise api.CableCoreError('No cableCORE session for this user; they need to sign in again.')
    out = await _call(ctx, 'execute', {'chatId': ctx.chat_id, 'code': code})
    approved: list[dict[str, Any]] = []
    for _ in range(MAX_PAUSES):
        if out.get('status') != 'paused':
            break
        pending: list[dict[str, Any]] = list(out.get('pending') or [])
        answer = await ctx.ask(await _approval_card(ctx, pending))
        if answer.approved is not True:
            await _call(ctx, 'reject', {'chatId': ctx.chat_id, 'executionId': out['executionId'], 'seq': int(pending[0].get('seq', 0))})
            done = [f"{c.get('method')}" for c in out.get('calls') or [] if c.get('state') == 'applied']
            return json.dumps({'status': 'declined', 'not_run': [p.get('method') for p in pending], 'ran_before': done,
                               'note': 'The user did not approve these changes; the run ended there.'}, ensure_ascii=False)
        approved += pending
        out = await _call(ctx, 'approve', {'chatId': ctx.chat_id, 'executionId': out['executionId']})
    calls: list[dict[str, Any]] = list(out.get('calls') or [])
    if approved:
        changed = {(p.get('connector'), p.get('method')) for p in approved}
        writes = [c for c in calls if (c.get('connector'), c.get('method')) in changed]
        after = await _read_back(writes)
        for c in writes:
            ctx.writes.append(WriteRecord(
                at=time.time(), action=str(c.get('method')), risk='write', method='', path=str(c.get('method')), query={},
                request=_clip(c.get('args'), 2000), status='done' if c.get('state') == 'applied' else str(c.get('state') or 'failed'),
                response=_clip(c.get('error') or c.get('result'), 3000)))
        if ctx.writes and after:
            ctx.writes[-1].after = after
    return json.dumps({
        'status': out.get('status'),
        'result': out.get('result'),
        **({'error': out['error']} if out.get('error') else {}),
        **({'logs': out['logs'][-20:]} if out.get('logs') else {}),
        'calls': [{k: c.get(k) for k in ('method', 'state', 'error') if c.get(k)} for c in calls][:40],
    }, ensure_ascii=False, default=str)[:RESULT_CHARS]


def codemode_tool() -> Tool:
    t = tool(cc_code, sequential=True)  # may wait on the user (approval card)
    data = ', `dc` (search, get, related, formula: the data tools, same inputs)' if _scope() == 'all' else ''
    return replace(t, description=t.description.replace('{dc}', data))
