"""Load and multi-user checks against the live stack (BE, MES, LLM), through the real agent loop.

    docker run ... python /ev/load_test.py        (same env as the evaluation: .env + .env.eval, EVAL_USER_EMAIL = a user with a MES token)

1. CONCURRENCY WITH GROUND TRUTH: 24 chat turns at once ('status, machine and quantity of job X', 12 real jobs, each asked twice).
   Each answer is checked against the MES's own record fetched directly: an answer that names another job's machine/status is a mix-up.
2. DRAWINGS: 6 simultaneous drawing requests as the user (the drawing service renders in a pool of 3).
3. A USER WITHOUT A MES TOKEN: must get no MES tools and must not state plant facts.
"""

import asyncio
import os
import re
import statistics
import time

import aiohttp

from ai import http
from ai.apps.mes import client as mes
from ai.apps.cablecore import api
from ai.apps.cablecore.evals import run as ev
from ai.context import TurnContext

WORKERS = int(os.getenv('LOAD_CONCURRENCY', '24'))
DESIGN = os.getenv('LOAD_DESIGN_ID', '5c57d00f-2a09-4945-99b3-b681e2a835ed')


def ctx(email: str) -> TurnContext:
    return TurnContext(user_id='load', user_email=email, cc_token='', chat_id='load', wait_answer=None)  # type: ignore[arg-type]


async def ground_truth(user: str) -> dict[str, dict[str, str]]:
    tools = {t.name: t for t in await mes.tools(ctx(user))}
    codes: list[str] = []
    for status in ('InProgress', 'Ready', 'New'):
        text = await tools['mes_list_jobs'].fn(ctx(user), Status=status)
        codes += re.findall(r'^- (\S+) \| site', text, re.MULTILINE)
    truth: dict[str, dict[str, str]] = {}
    for code in dict.fromkeys(codes):
        header = await tools['mes_get_job'].fn(ctx(user), JobCode=code)
        machine, status = re.search(r'Machine: ([^.|]+?)[.|]', header), re.search(r'status (\w+)', header)
        if machine and status:
            truth[code] = {'machine': machine.group(1).strip(), 'status': status.group(1)}
        if len(truth) == 12:
            break
    return truth


async def main() -> None:
    user = os.environ['EVAL_USER_EMAIL']
    token = await ev.login()
    truth = await ground_truth(user)
    print(f'ground truth: {len(truth)} real jobs read straight from the MES')

    # ---- 1. concurrency with ground truth
    cases = [{'id': f'L{i:02d}-{code}', 'turns': [f'What is the status, machine and required quantity of job {code}? One short line.'], 'approve': False}
             for i, code in enumerate(list(truth) * 2)][:WORKERS]
    sem = asyncio.Semaphore(WORKERS)
    started = time.time()
    results = await asyncio.gather(*(ev.run_case(c, token, sem) for c in cases))
    wall = time.time() - started
    ok = wrong = errors = 0
    lat = []
    for case, res in zip(cases, results):
        code = case['id'].split('-', 1)[1]
        turn = res['turns'][0]
        lat.append(turn['latency_s'])
        answer = (turn.get('answer') or '').lower()
        if turn.get('error') or not answer:
            errors += 1
        elif truth[code]['machine'].lower() in answer and not any(  # right machine; and no OTHER job's machine named
                m['machine'].lower() in answer for c, m in truth.items() if c != code and m['machine'].lower() != truth[code]['machine'].lower()):
            ok += 1
        else:
            wrong += 1
            print(f"  MISMATCH {code}: expected machine {truth[code]['machine']!r}; answer: {answer[:140]!r}")
    lat.sort()
    print(f'1) {len(cases)} concurrent turns in {wall:.0f}s: correct {ok}, wrong/mixed-up {wrong}, errors {errors} | latency median {statistics.median(lat):.0f}s p90 {lat[int(len(lat) * .9) - 1]:.0f}s max {lat[-1]:.0f}s')

    # ---- 2. drawings in parallel
    async def draw() -> tuple[int, float, int]:
        t = time.time()
        async with http.session().get(f'{api.CC_API_URL}/erp/{DESIGN}/drawingurl', headers={'Authorization': f'Bearer {token}'},
                                      timeout=aiohttp.ClientTimeout(total=180)) as r:
            body = await r.read()
            return r.status, time.time() - t, len(body)

    drawings = await asyncio.gather(*(draw() for _ in range(6)), return_exceptions=True)
    good = [d for d in drawings if isinstance(d, tuple) and d[0] == 200 and d[2] > 10_000]
    print(f"2) 6 parallel drawings: {len(good)} ok | " + ', '.join(f'{d[0]} {d[1]:.1f}s' if isinstance(d, tuple) else type(d).__name__ for d in drawings))

    # ---- 3. a user with no MES token
    os.environ['EVAL_USER_EMAIL'] = 'nobody-without-a-token@example.com'
    code = next(iter(truth))
    probe = {'id': 'NOTOKEN', 'turns': [f'What is the status and machine of job {code}?'], 'approve': False}
    res = await ev.run_case(probe, token, asyncio.Semaphore(1))
    turn = res['turns'][0]
    used = [t['name'] for t in turn['tools'] if t['name'].startswith('mes_')]
    leaked = truth[code]['machine'].lower() in (turn.get('answer') or '').lower()
    print(f"3) user without a MES token: MES tools used {used or 'none'} | states the job's machine as fact: {leaked} | answer: {(turn.get('answer') or '')[:200]!r}")
    await http.close()


asyncio.run(main())
