"""Route search regression: replay the cc_find queries of a recorded eval run against the index.

For each case, where did the routes the model finally used (cc_describe / cc_action ids) rank in its
cc_find results? Needs a saved OpenAPI document and an eval results file, so it is skipped without them:
    CC_OPENAPI_FILE=openapi.json CC_EVAL_RESULTS=uc145.json pytest tests/test_find_replay.py -s
Capabilities need a login and are left out, so described routes rank a bit lower than live.
Baseline (OWUI run, 2026-11-12): 84 routes, #1: 26, top 5: 57, not in top 20: 12.
"""

import asyncio
import json
import os
import re

import pytest

from ai.apps.cablecore import api, index

SPEC_FILE, RESULTS_FILE = os.getenv('CC_OPENAPI_FILE'), os.getenv('CC_EVAL_RESULTS')
BASELINE_TOP5 = 57


@pytest.mark.skipif(not (SPEC_FILE and RESULTS_FILE), reason='set CC_OPENAPI_FILE and CC_EVAL_RESULTS')
def test_find_replay(monkeypatch):
    assert SPEC_FILE and RESULTS_FILE
    spec = json.load(open(SPEC_FILE, encoding='utf-8'))

    async def _spec(_token):
        return spec

    async def _caps(_token):
        return {}

    monkeypatch.setattr(api, 'spec', _spec)
    monkeypatch.setattr(api, 'capabilities', _caps)
    monkeypatch.setattr(index, '_bm25', None)
    asyncio.run(index.ensure('x'))

    used_total = hit1 = hit5 = miss = 0
    for case in json.load(open(RESULTS_FILE, encoding='utf-8')):
        queries, used = [], []
        for t in case['turns'][-1].get('tools') or []:
            raw = t.get('args') if isinstance(t.get('args'), str) else json.dumps(t.get('args') or {})
            if t['name'] == 'cc_find':
                m = re.search(r'"query"\s*:\s*"((?:[^"\\]|\\.)*)', raw)
                queries.append(m.group(1) if m else '')
            elif t['name'] in ('cc_describe', 'cc_action'):  # args are cut off in the log: ids by pattern
                used += re.findall(r'"(?:id|name)"\s*:\s*"([^"]+)"', raw)
        used = [u for u in dict.fromkeys(used) if u and index.get(u)]
        if not queries:
            continue
        for route in used:
            ranks = [ids.index(route) + 1 for q in queries if route in (ids := [r.id for r in index.find(q, 20)])]
            best = min(ranks) if ranks else None
            used_total += 1
            hit1 += best == 1
            hit5 += best is not None and best <= 5
            miss += best is None
    print(f'routes used after cc_find: {used_total}  #1: {hit1}  top 5: {hit5}  not in top 20: {miss}')
    assert hit5 >= BASELINE_TOP5, 'route search got worse than the OWUI baseline'
