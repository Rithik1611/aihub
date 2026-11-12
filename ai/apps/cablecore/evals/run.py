"""Run eval cases straight through the agent (no backend): record answer, tools, cards, latency.

    uv run --env-file .env python -m ai.apps.cablecore.evals.run <out.json> [concurrency] [case ids...]

Needs EVAL_USER / EVAL_PASS (a cableCORE login: it ends that user's browser session), the cableCORE
backend (CC_API_URL, OPENAPI_DOCS=true), the database (CC_DB_URL) and the sandbox. Every change is
DECLINED unless a case says "approve": true; questions get their first option.
Output matches the OWUI runs, so grade.py and the tracker export work on it unchanged.
"""

import asyncio
import json
import os
import pathlib
import sys
import time

import aiohttp

from ai import TurnContext, http, run_turn, sandbox, user_message
from ai.context import Answer, ApprovalCard, Card, Done, Error, Items, TextDelta
from ai.settings import settings
from ai.apps.cablecore import api

HERE = pathlib.Path(__file__).parent
FILES = pathlib.Path(os.getenv('EVAL_FILES', 'C:/DDM/ai-demo-files'))


async def login() -> str:
    async with aiohttp.ClientSession() as s:
        async with s.post(f'{api.CC_API_URL}/auth/token', json={'username': os.environ['EVAL_USER'], 'password': os.environ['EVAL_PASS']}) as r:
            body = await r.json()
            if r.status >= 400:
                raise SystemExit(f'login failed: {r.status}')  # never retried: 3 failures lock the account
            return body['data']['accessToken']


class Tickets:
    async def issue(self, user_id: str, chat_id: str) -> dict:
        return {'error': 'no sandbox gateway in eval runs'}


def model_calls(items: list[dict]) -> int:
    """Model requests in a turn: one per round of tool calls, plus the final answer."""
    rounds, in_outputs = 0, False
    for i in items:
        if i['type'] == 'function_call_output' and not in_outputs:
            rounds, in_outputs = rounds + 1, True
        elif i['type'] != 'function_call_output':
            in_outputs = False
    return rounds + 1


def summarize(items: list[dict]) -> tuple[str, list[dict]]:
    status = {i['call_id']: ('failed' if i['output'].startswith('{"error"') else 'completed') for i in items if i['type'] == 'function_call_output'}
    calls = [{'name': i['name'], 'status': status.get(i['call_id']), 'args': i.get('arguments', '')[:240]} for i in items if i['type'] == 'function_call']
    text = '\n'.join(p.get('text', '') for i in items if i['type'] == 'message' and i.get('role') == 'assistant'
                     for p in i.get('content') or [] if p.get('type') == 'output_text')
    return text, calls


async def run_case(case: dict, token: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        history: list[dict] = []
        turns = []
        for n, prompt in enumerate(case['turns']):
            confirms: list[str] = []

            async def wait_answer(card_id: str, card: Card) -> Answer:
                if isinstance(card, ApprovalCard):
                    confirms.append(card.markdown[:300])
                    return Answer(approved=bool(case.get('approve')))
                confirms.append('ASK_USER: ' + ' | '.join(f"{q.question} [{', '.join(o.label for o in q.options)}]" for q in card.questions))
                return Answer(status='answered', answers={q.id: q.options[0].label for q in card.questions})

            ctx = TurnContext(user_id='eval', user_email='eval@cablecore.local', cc_token=token, chat_id=f"eval-{case['id']}",
                              wait_answer=wait_answer, tickets=Tickets())
            attachment = None
            if n == 0 and case.get('file'):
                path = await sandbox.save_file(case['file'], (FILES / case['file']).read_bytes(), 'application/octet-stream', folder='uploads')
                attachment = (case['file'], path)
            message = user_message(prompt, attachment, case.get('page', '/design'))
            t0, first_token, answered, error, items, usage = time.time(), None, None, None, [], {}  # answered: last streamed text
            async for e in run_turn(ctx, history, message):
                if isinstance(e, TextDelta):
                    first_token = first_token or time.time() - t0
                    answered = time.time() - t0
                elif isinstance(e, Error):
                    error = e.message
                elif isinstance(e, Items):
                    items = e.items
                elif isinstance(e, Done):
                    usage = e.usage
            history += items
            text, calls = summarize(items)
            latency = time.time() - t0
            turns.append({'prompt': prompt, 'latency_s': round(latency, 1), 'answer_s': round(answered or latency, 1), 'checked': True,
                          'first_token_s': round(first_token, 1) if first_token else None, 'error': error, 'answer': text,
                          'tools': calls, 'confirmations': confirms, 'mode': settings().cc_tool_mode,
                          'model_calls': model_calls(items), 'usage': usage})
            print(f"{case['id']} {latency:.1f}s model_calls={model_calls(items)} tools={len(calls)} "
                  f"tokens={usage.get('input_tokens', 0)}+{usage.get('output_tokens', 0)} err={error}", flush=True)
        return {'id': case['id'], 'group': case.get('group'), 'chat_id': f"eval-{case['id']}", 'turns': turns}


async def main() -> None:
    out, concurrency, ids = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 3, set(sys.argv[3:])
    cases = [c for c in json.load(open(os.getenv('EVAL_CASES', HERE / 'cases.json'), encoding='utf-8')) if not ids or c['id'] in ids]
    token = await login()
    sem = asyncio.Semaphore(concurrency)
    results = await asyncio.gather(*(run_case(c, token, sem) for c in cases))
    json.dump(results, open(out, 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
    await http.close()


if __name__ == '__main__':
    asyncio.run(main())
