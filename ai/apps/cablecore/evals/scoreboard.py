"""Per-case cost of an eval run, read back from Langfuse: steps, tool calls, tokens (cached / uncached), time, grade.

    uv run --env-file .env python -m ai.apps.cablecore.evals.scoreboard <since>[..<until>] [results.graded.json] [out.json]

Run the eval with LANGFUSE_TRACING=true, note the time it started, then pass it here. Eval chats are the
Langfuse sessions named eval-<case id>; every trace in them after <since> counts.
Uncached input = input + cache writes: what each step really costs. Cached input is re-sent prefix.
"""

import json
import os
import sys
from collections import defaultdict
from typing import Any

import httpx

FIELDS = 'core,basic,usage,io'


def observations(since: str, until: str = '') -> list[dict[str, Any]]:
    base, auth = os.environ['LANGFUSE_BASE_URL'], (os.environ['LANGFUSE_PUBLIC_KEY'], os.environ['LANGFUSE_SECRET_KEY'])
    out: list[dict[str, Any]] = []
    cursor = None
    while True:
        params = {'fromStartTime': since, 'fields': FIELDS, 'limit': 100, **({'toStartTime': until} if until else {}),
                  **({'cursor': cursor} if cursor else {})}
        page = httpx.get(f'{base}/api/public/v2/observations', params=params, auth=auth, timeout=60).json()
        out += page.get('data') or []
        cursor = (page.get('meta') or {}).get('cursor')
        if not cursor or not page.get('data'):
            return out


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def score(since: str, graded_path: str | None = None) -> list[dict[str, Any]]:
    obs = observations(*since.split('..', 1))
    session_of = {o['traceId']: o.get('sessionId') for o in obs if o.get('sessionId')}
    cases: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(float))
    for o in obs:
        session = session_of.get(o['traceId']) or ''
        if not session.startswith('eval-'):
            continue
        c = cases[session.removeprefix('eval-')]
        if o.get('type') == 'AGENT':
            c['seconds'] += float(o.get('latency') or 0)
            c['turns'] += 1
            continue
        if o.get('type') != 'GENERATION':
            continue
        u = _json(o.get('usageDetails')) or {}
        c['cached'] += u.get('input_cached_tokens', 0)
        c['uncached'] += u.get('input', 0) + u.get('input_cache_write_tokens', 0)
        c['output'] += u.get('output', 0)
        c['reasoning'] += u.get('output_reasoning_tokens', 0)
        inp = _json(o.get('input'))
        if isinstance(inp, dict) and 'tools' in inp:  # an agent step (checks and titles have no tools)
            c['steps'] += 1
            c['tools_shown'] = max(c['tools_shown'], len(inp.get('tools') or []))
            out = _json(o.get('output'))
            items = out.get('output') if isinstance(out, dict) else out
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and item.get('type') == 'function_call':
                    c['tool_calls'] += 1
                    if item.get('name') == 'load_skill':
                        c['skills'] += 1
                elif isinstance(item, dict) and item.get('type') == 'tool_search_call':
                    c['searches'] += 1
    grades: dict[str, str] = {}
    if graded_path:
        with open(graded_path, encoding='utf-8') as f:
            grades = {r['id']: r['result'] for r in json.load(f)}
    rows = []
    for case_id in sorted(cases):
        c = cases[case_id]
        rows.append({'id': case_id, 'grade': grades.get(case_id, ''), **{k: round(v, 1) if k == 'seconds' else int(v) for k, v in c.items()}})
    return rows


def table(rows: list[dict[str, Any]]) -> str:
    cols = ['id', 'grade', 'steps', 'tool_calls', 'searches', 'skills', 'tools_shown', 'uncached', 'cached', 'output', 'reasoning', 'seconds']
    lines = [' | '.join(cols), ' | '.join('---' for _ in cols)]
    lines += [' | '.join(str(r.get(k, 0)) for k in cols) for r in rows]
    total = {k: sum(r.get(k, 0) for r in rows) for k in cols[2:]}
    passed = sum(1 for r in rows if r.get('grade') == 'pass')
    lines.append(' | '.join(['TOTAL', f'{passed} pass'] + [str(round(total[k], 1)) for k in cols[2:]]))
    return '\n'.join(lines)


if __name__ == '__main__':
    result = score(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print(table(result))
    if len(sys.argv) > 3:
        with open(sys.argv[3], 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=1)
