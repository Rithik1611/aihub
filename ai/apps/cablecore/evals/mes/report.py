"""MES evaluation report: use cases + cases + graded results -> one Excel workbook.

    uv run --no-project --with openpyxl python report.py <use_cases.json> <cases.json> <results.json> <results.graded.json> <out.xlsx>

Two scores per use case, because a correct refusal is good behaviour but not a delivered use case:
  Delivered?      delivered | partly | not supported   (did the system do what the use case describes, with data it can reach)
  Answer quality  correct | flawed                     (were its statements true and grounded, arithmetic right, honest about gaps)
Sheets: Summary (every count is a COUNTIF/COUNTIFS formula over Results), Results, Gaps (not fully delivered), Flawed answers (the bugs to fix).
"""

import json
import sys

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

use_cases = {u['id']: u for u in json.load(open(sys.argv[1], encoding='utf-8'))}
cases = {c['id']: c for c in json.load(open(sys.argv[2], encoding='utf-8'))}
results = {r['id']: r for r in json.load(open(sys.argv[3], encoding='utf-8'))}
graded = {g['id']: g for g in json.load(open(sys.argv[4], encoding='utf-8'))}

DELIVERY = ['delivered', 'partly', 'not supported']
QUALITY = ['correct', 'flawed']
FILL = {'delivered': 'C6EFCE', 'partly': 'FFEB9C', 'not supported': 'D9D9D9', 'correct': 'C6EFCE', 'flawed': 'FFC7CE'}
ARIAL, BOLD = Font(name='Arial', size=10), Font(name='Arial', size=10, bold=True)
HEAD = PatternFill('solid', fgColor='1F3864')

wb = openpyxl.Workbook()
summary, res = wb.active, wb.create_sheet('Results')
summary.title = 'Summary'
gaps, flawed = wb.create_sheet('Gaps'), wb.create_sheet('Flawed answers')


def header(ws, row, cols, start=1):
    for i, name in enumerate(cols, start):
        c = ws.cell(row=row, column=i, value=name)
        c.font, c.fill, c.alignment = Font(name='Arial', size=10, bold=True, color='FFFFFF'), HEAD, Alignment(wrap_text=True, vertical='center')


def put(ws, row, values, fills=None):
    for col, v in enumerate(values, 1):
        cell = ws.cell(row=row, column=col, value=v)
        cell.font, cell.alignment = ARIAL, Alignment(wrap_text=True, vertical='top')
    for col, key in (fills or {}).items():
        ws.cell(row=row, column=col).fill = PatternFill('solid', fgColor=FILL.get(key, 'FFFFFF'))


def widths(ws, ws_widths):
    for col, w in enumerate(ws_widths, 1):
        ws.column_dimensions[get_column_letter(col)].width = w


# ---- Results: A ID, B Area, C Use case, D Impl, E Phase, F Prompt, G Delivered?, H Answer quality, I Reviewer verdict,
#              J What it did, K Why, L MES tools, M Latency, N Case check
COLS = ['ID', 'Area', 'AI use case', 'Implementation type', 'Phase', 'Prompt asked', 'Delivered?', 'Answer quality', 'Reviewer verdict',
        'What the assistant did', 'Why', 'MES tools called', 'Latency (s)', 'What a good answer needed']
header(res, 1, COLS)
order = list(use_cases)
rows = []
for r, uid in enumerate(order, 2):
    u, c, g = use_cases[uid], cases.get(uid, {}), graded.get(uid, {})
    run = results.get(uid)
    turn = run['turns'][-1] if run else {}
    tools = ', '.join(sorted({t['name'] for t in turn.get('tools') or [] if t['name'].startswith('mes_')})) or ('(none)' if run else 'not run')
    delivery, quality = g.get('delivery', 'not run'), g.get('behaviour', 'not run')
    values = [uid, u['area'], u['name'], u['impl'], u['phase'], (c.get('turns') or [''])[0], delivery, quality, g.get('result', 'not run'),
              g.get('summary', ''), g.get('reason', ''), tools, turn.get('latency_s'), c.get('check', '')]
    put(res, r, values, {7: delivery, 8: quality})
    rows.append(values)
widths(res, [8, 22, 30, 22, 22, 55, 14, 14, 14, 55, 45, 28, 10, 50])
res.freeze_panes = 'D2'
res.auto_filter.ref = f'A1:{get_column_letter(len(COLS))}{len(order) + 1}'
last = len(order) + 1

# ---- Summary (formulas over Results)
summary['A1'], summary['A1'].font = 'cableAI - MES use case evaluation', Font(name='Arial', size=14, bold=True)
summary['A2'], summary['A2'].font = (f'{len(order)} MES use cases from the workbook, each tested end to end through the real chat agent with the live MES '
                                     'tools. Scored by an LLM reviewer and spot-checked by hand. All counts are formulas over the Results sheet.'), ARIAL
summary['A3'], summary['A3'].font = ('Delivered? = did the system do what the use case describes, with data it can reach. '
                                     'Answer quality = were its statements true, grounded and honest about gaps. A correct refusal is "not supported / correct".'), ARIAL

header(summary, 5, ['Delivered?', 'Use cases', '% of all'])
for i, v in enumerate(DELIVERY, 6):
    summary.cell(row=i, column=1, value=v).font = ARIAL
    summary.cell(row=i, column=1).fill = PatternFill('solid', fgColor=FILL[v])
    summary.cell(row=i, column=2, value=f'=COUNTIF(Results!$G$2:$G${last},A{i})').font = ARIAL
    c = summary.cell(row=i, column=3, value=f'=IFERROR(B{i}/$B$9,0)')
    c.font, c.number_format = ARIAL, '0.0%'
summary.cell(row=9, column=1, value='Total').font = BOLD
summary.cell(row=9, column=2, value='=SUM(B6:B8)').font = BOLD

header(summary, 11, ['Answer quality', 'Use cases', '% of all'])
for i, v in enumerate(QUALITY, 12):
    summary.cell(row=i, column=1, value=v).font = ARIAL
    summary.cell(row=i, column=1).fill = PatternFill('solid', fgColor=FILL[v])
    summary.cell(row=i, column=2, value=f'=COUNTIF(Results!$H$2:$H${last},A{i})').font = ARIAL
    c = summary.cell(row=i, column=3, value=f'=IFERROR(B{i}/$B$9,0)')
    c.font, c.number_format = ARIAL, '0.0%'

header(summary, 15, ['Delivered? x quality'] + QUALITY)
for i, d in enumerate(DELIVERY, 16):
    summary.cell(row=i, column=1, value=d).font = ARIAL
    for j, q in enumerate(QUALITY, 2):
        summary.cell(row=i, column=j, value=f'=COUNTIFS(Results!$G$2:$G${last},$A{i},Results!$H$2:$H${last},"{q}")').font = ARIAL


def breakdown(start, title, col_letter, keys):
    summary.cell(row=start, column=1, value=title).font = BOLD
    header(summary, start + 1, ['Group'] + DELIVERY + ['Flawed answers', 'Total'])
    for i, k in enumerate(keys, start + 2):
        summary.cell(row=i, column=1, value=k).font = ARIAL
        for j, v in enumerate(DELIVERY, 2):
            summary.cell(row=i, column=j, value=f'=COUNTIFS(Results!${col_letter}$2:${col_letter}${last},$A{i},Results!$G$2:$G${last},"{v}")').font = ARIAL
        summary.cell(row=i, column=5, value=f'=COUNTIFS(Results!${col_letter}$2:${col_letter}${last},$A{i},Results!$H$2:$H${last},"flawed")').font = ARIAL
        summary.cell(row=i, column=6, value=f'=COUNTIF(Results!${col_letter}$2:${col_letter}${last},$A{i})').font = BOLD
    return start + 3 + len(keys)


row = 21
row = breakdown(row, 'By area', 'B', sorted({u['area'] for u in use_cases.values()}))
row = breakdown(row, 'By implementation type', 'D', sorted({u['impl'] for u in use_cases.values()}))
row = breakdown(row, 'By phase', 'E', sorted({u['phase'] for u in use_cases.values()}))
summary.column_dimensions['A'].width = 44
for col in 'BCDEF':
    summary.column_dimensions[col].width = 16

# ---- Gaps and Flawed answers
header(gaps, 1, ['ID', 'AI use case', 'Area', 'Delivered?', 'Why', 'What the assistant did'])
n = 2
for v in rows:
    if v[6] != 'delivered':
        put(gaps, n, [v[0], v[2], v[1], v[6], v[10], v[9]], {4: v[6]})
        n += 1
widths(gaps, [8, 34, 24, 14, 60, 70])
gaps.freeze_panes = 'A2'

header(flawed, 1, ['ID', 'AI use case', 'Prompt asked', 'Delivered?', 'What went wrong', 'What the assistant did'])
n = 2
for v in rows:
    if v[7] == 'flawed':
        put(flawed, n, [v[0], v[2], v[5], v[6], v[10], v[9]], {4: v[6]})
        n += 1
widths(flawed, [8, 30, 50, 14, 60, 70])
flawed.freeze_panes = 'A2'

wb.save(sys.argv[5])
from collections import Counter
print('saved', sys.argv[5])
print('delivered?:', dict(Counter(v[6] for v in rows)), '| quality:', dict(Counter(v[7] for v in rows)), '| flawed sheet rows:', n - 2)
