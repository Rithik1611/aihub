"""Grade the 145 use-case runs against each use case's expected functionality (no answer key).

usage: grade.py <tracker.xlsx> <cases.json> <results.json>  -> results.graded.json
Needs LUNA_BASE_URL / LUNA_API_KEY / LUNA_MODEL in the environment.
"""
import asyncio
import json
import os
import sys

import openpyxl
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

PROMPT = """You review a test of a cable-design assistant (DesignCenter chatbot) against one planned AI use case.

Use case: {name} - {expected}
User prompt: {prompt}
Tools the assistant called: {tools}
Approval cards (proposed data changes; in this test every change was DECLINED on purpose so no data changed): {cards}
Assistant's answer:
{answer}

Judge whether the system delivers this use case for this prompt:
- "pass": it did what the use case expects, grounded in the user's DesignCenter data where the prompt is about it (or correct
  engineering where it is general). For a change request, a correct, specific approval card counts as delivered.
- "partial": useful but incomplete, generic where data was needed, or an obvious gap/error.
- "fail": did not do it, wrong, error, or said it cannot.
- "not supported": the system says the capability or data does not exist in DesignCenter (e.g. no inventory/ERP, no machine data).
Write for a project manager: plain words, no tool names."""

class Verdict(BaseModel):
    result: Literal['pass', 'partial', 'fail', 'not supported']
    summary: str = Field(description='what the system did and found, 1-3 sentences')
    reason: str = Field(description='why this result, one sentence')


async def grade(client, sem, uc, case, result):
    turn = result['turns'][-1]
    err = ''
    async with sem:
        for _ in range(3):
            try:
                out = await client.responses.parse(
                    model=os.environ.get('LUNA_MODEL', 'gpt-5.6-luna'),
                    input=PROMPT.format(name=uc['name'], expected=uc['expected'], prompt=case['turns'][0],
                                        tools=', '.join(c['name'] for c in turn.get('tools') or []) or 'none',
                                        cards=json.dumps(turn.get('confirmations') or [], ensure_ascii=False)[:3000],
                                        answer=(turn.get('answer') or f"(no answer; error: {turn.get('error')})")[:15000]),
                    reasoning={'effort': 'low'}, store=False, text_format=Verdict)
                return {'id': case['id'], **out.output_parsed.model_dump()}
            except Exception as e:  # retry transient API/parse errors
                err = str(e)
        return {'id': case['id'], 'result': 'ungraded', 'summary': '', 'reason': err[:200]}


async def main():
    ws = openpyxl.load_workbook(sys.argv[1])['Master Catalogue']
    ucs = {r[0]: {'name': r[3], 'expected': r[4]} for r in ws.iter_rows(min_row=6, max_row=150, values_only=True)}
    cases = {c['id']: c for c in json.load(open(sys.argv[2], encoding='utf-8'))}
    results = json.load(open(sys.argv[3], encoding='utf-8'))
    client = AsyncOpenAI(base_url=os.environ['LUNA_BASE_URL'], api_key=os.environ['LUNA_API_KEY'])
    sem = asyncio.Semaphore(8)
    graded = await asyncio.gather(*(grade(client, sem, ucs[r['id']], cases[r['id']], r) for r in results))
    json.dump(graded, open(sys.argv[3].replace('.json', '.graded.json'), 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
    from collections import Counter
    print(Counter(g['result'] for g in graded))


asyncio.run(main())
