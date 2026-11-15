"""Replays 'job X' questions ONE AT A TIME for jobs that exist in the MES, to tell a concurrency problem from a lookup problem."""

import asyncio
import os

from ai import http
from ai.apps.cablecore.evals import run as ev

CODES = os.getenv('PROBE_CODES', 'POT5246,TEST-ALLOCATE,PO007679').split(',')


async def main() -> None:
    token = await ev.login()
    for code in CODES:
        case = {'id': f'ALONE-{code}', 'turns': [f'What is the status, machine and required quantity of job {code}? One short line.'], 'approve': False}
        turn = (await ev.run_case(case, token, asyncio.Semaphore(1)))['turns'][0]
        print(f'--- {code} ALONE: {turn["latency_s"]}s')
        for t in turn['tools']:
            print(f'    {t["name"]}({t["args"][:120]}) -> {t["status"]}')
        print('    answer:', (turn.get('answer') or '')[:160].replace('\n', ' '))
    await http.close()


asyncio.run(main())
