"""Grade the 145 use-case runs against each use case's expected functionality (no answer key).

usage: grade.py <tracker.xlsx> <cases.json> <results.json>  -> results.graded.json
Needs LUNA_BASE_URL / LUNA_API_KEY / LUNA_MODEL in the environment.
"""
import asyncio
import json
import os
import re
import sys

from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

SUBJECT = os.environ.get('GRADER_SUBJECT', 'a cable-design assistant (DesignCenter chatbot)')

PROMPT = """You review a test of {subject} against one planned AI use case.

Use case: {name} - {expected}
User prompt: {prompt}
Case-specific check (what a good answer must do or avoid; empty = none): {check}
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
Also answer two separate questions, because a correct refusal is good behaviour but not a delivered use case:
- delivery: did the system deliver what the use case itself describes, using data it can reach? "delivered" = yes; "partly" = some of it;
  "not supported" = the data or capability the use case needs does not exist within its reach, however well it explained that.
- behaviour: independent of delivery, were its statements true and grounded? "correct" = every claim is backed by a successful tool result or is
  clearly labelled general knowledge, arithmetic is right, and it was honest about gaps. "flawed" = invented or speculative claims presented as
  findings, wrong arithmetic, misleading about what it checked or could check, or a tool misused.
Write for a project manager: plain words, no tool names."""

# Source code in a reply (the agent may read DesignCenter's code, never show it): a source
# file path, or TypeScript syntax.
CODE = re.compile(r'\b[\w./-]+\.(?:ts|tsx|prisma)\b|\bsrc/|^\s*(?:export|import) |=>\s*\{|@(?:Injectable|Controller|Get|Post)\(', re.MULTILINE)


class Verdict(BaseModel):
    result: Literal['pass', 'partial', 'fail', 'not supported']
    delivery: Literal['delivered', 'partly', 'not supported']
    behaviour: Literal['correct', 'flawed']
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
                    input=PROMPT.format(subject=SUBJECT, check=case.get('check') or '', name=uc['name'], expected=uc['expected'], prompt=case['turns'][0],
                                        tools=', '.join(c['name'] for c in turn.get('tools') or []) or 'none',
                                        cards=json.dumps(turn.get('confirmations') or [], ensure_ascii=False)[:3000],
                                        answer=(turn.get('answer') or f"(no answer; error: {turn.get('error')})")[:15000]),
                    reasoning={'effort': 'low'}, store=False, text_format=Verdict)
                return {'id': case['id'], **out.output_parsed.model_dump(), 'code_shown': bool(CODE.search(turn.get('answer') or ''))}
            except Exception as e:  # retry transient API/parse errors
                err = str(e)
        return {'id': case['id'], 'result': 'ungraded', 'summary': '', 'reason': err[:200]}


async def main():
    if sys.argv[1].endswith('.json'):  # use_cases.json: [{id, name, expected, ...}] (the MES evaluation)
        ucs = {u['id']: {'name': u['name'], 'expected': u['expected']} for u in json.load(open(sys.argv[1], encoding='utf-8'))}
    else:
        import openpyxl  # only the old Excel tracker needs it; not in the aihub image

        ws = openpyxl.load_workbook(sys.argv[1])['Master Catalogue']
        ucs = {r[0]: {'name': r[3], 'expected': r[4]} for r in ws.iter_rows(min_row=6, max_row=150, values_only=True)}
    cases = {c['id']: c for c in json.load(open(sys.argv[2], encoding='utf-8'))}
    results = json.load(open(sys.argv[3], encoding='utf-8'))
    client = AsyncOpenAI(base_url=os.environ['LUNA_BASE_URL'], api_key=os.environ['LUNA_API_KEY'])
    sem = asyncio.Semaphore(8)
    graded = await asyncio.gather(*(grade(client, sem, ucs.get(r['id']) or cases[r['id']], cases[r['id']], r) for r in results))
    json.dump(graded, open(sys.argv[3].replace('.json', '.graded.json'), 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
    from collections import Counter
    print(Counter(g['result'] for g in graded), 'code shown in', [g['id'] for g in graded if g.get('code_shown')])


asyncio.run(main())
