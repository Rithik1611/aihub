"""Builds MES_Data_Request.xlsx: which MES data each of the 84 use cases needs (counts are formulas)."""

import json
import sys

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HERE = '/ev/'
ITEMS = {  # id: (name, what exactly, example fields, MES-side action)
    'D1': ('Approved recipes and process setpoints', 'Per item (and machine/extruder): the approved settings, readable through a tool',
           'zone 1-3 / crosshead / die temperatures, screw RPM, line speed, melt pressure, cooling-water temperature, tension; unit and tolerance for each', 'Expose recipe/indication-sheet lines as a tool (today only the document NAME is returned)'),
    'D2': ('Recipe versions and approval history', 'Revision number, status, who approved and when, change reason',
           'recipe id, revision, approved by/at, pending changes, deviation from previous revision', 'Expose recipe revision + approval records'),
    'D3': ('Live / recorded process readings', 'Time-series or last value from the machines (PLC/SCADA/historian)',
           'actual melt temperature, melt pressure, speed, OD, eccentricity, tension, alarms, with timestamp per job/lot', 'Tool to read readings by job/machine and time window'),
    'D4': ('Quality results, limits and defects', 'Lab/inline test results with limits and defect/scrap codes per job or lot',
           'measured OD, wall thickness, eccentricity, spark test, cure test (hot set), roughness; min/max limits; defect code, scrap qty and reason', 'Expose quality results and their limits (today: quality orders only, no measurements)'),
    'D5': ('Tooling master and usage', 'Dies, tips, crossheads: dimensions, stock, location, usage and wear, cost, which items/machines they fit',
           'die/tip id, bore/land dimensions, compatible machine + item, metres run, last service, stock on hand, unit cost', 'Expose tooling inventory and its usage history'),
    'D6': ('Machine capability and limits', 'What each machine can physically run',
           'max/min speed, screw diameter and L/D, number of zones, reel size / diameter / weight limits, tension range, cooling trough length, supported processes and items', 'Add capability fields to the machine record / tool'),
    'D7': ('Approved SOP and instruction content', 'The TEXT of the approved procedures, not only their titles',
           'startup, setup, changeover, shutdown and purging, first-off inspection, monitoring checks, material drying/handling; version + language', 'Let the knowledge search return document contents (today: names only)'),
    'D8': ('Operators: roles, skills, languages, acknowledgements', 'Who is allowed/trained for what and what they have acknowledged',
           'operator id, role, skill/certification per machine, preferred language, instruction acknowledgements with time', 'Expose operator and acknowledgement records'),
    'D9': ('Actual times and speed history', 'What really happened, to compare with the plan',
           'actual start/end, actual duration (with unit), setup and changeover minutes, downtime, achieved speed per job, history by item/machine', 'Add actuals and a unit to durations (today: planned duration without a unit)'),
    'D10': ('Cost data', 'Planned vs actual cost per job and per tooling item',
            'material, labour, machine-hour rate, scrap cost, die replacement cost', 'Expose cost fields (none exist in the reachable records)'),
    'D11': ('Material and lot data', 'Compound/grade per item, lots reserved to jobs, stock on hand by location',
            'compound grade and supplier, lot code, quantity, expiry/drying state, reservation per job, shortage flag', 'Link BOM material to lots and stock; expose grade'),
    'D12': ('Production order, job and design link', 'A reliable key from design -> production order -> job',
            'design/item id on the order, order id format matching the jobs (e.g. PO007700_1 vs PO0077), operation list per order', 'Fix lookups: a job code is not found by production-order id and vice versa'),
    'D13': ('Job quantities and units', 'Required / completed / good / error with a unit on every figure',
            'qty required, completed, good, error, unit (m, pcs), duration unit', 'Always return units; fill good/error quantities'),
}
USES = {  # use case id -> items it needs
    'N021': 'D1 D2 D4', 'N022': 'D1 D3 D4', 'N023': 'D3 D4 D12', 'N024': 'D1 D3 D4', 'N025': 'D1 D3 D4', 'N026': 'D1 D3 D12', 'N027': 'D1 D6 D9',
    'N028': 'D4 D12 D13', 'N029': 'D4 D13', 'N030': 'D5 D6 D9', 'N042': 'D6 D9 D12', 'N043': 'D11 D12', 'N061': 'D1 D6 D9', 'N062': 'D1 D4 D6',
    'N063': 'D1 D9 D12', 'N064': 'D1 D6 D12', 'N065': 'D1 D6 D12', 'N066': 'D1 D12', 'N067': 'D1 D12', 'N068': 'D1 D9', 'N069': 'D1 D9', 'N070': 'D13',
    'N071': 'D9 D13', 'N072': 'D5 D9', 'N073': 'D9', 'N074': 'D6 D9', 'N075': 'D6 D9', 'N076': 'D1 D4 D6', 'N077': 'D5 D12', 'N078': 'D5 D12',
    'N079': 'D5 D12', 'N080': 'D1 D5', 'N081': 'D5 D12', 'N082': 'D5 D12', 'N083': 'D1 D5', 'N084': 'D1 D11', 'N085': 'D5 D6', 'N086': 'D5',
    'N087': 'D5 D9', 'N088': 'D4 D5', 'N089': 'D5 D10', 'N090': 'D5', 'N102': 'D10', 'N106': 'D6', 'N107': 'D6', 'N108': 'D1 D2', 'N109': 'D5 D12',
    'N110': 'D1 D6', 'N111': 'D1 D11', 'N112': 'D1', 'N113': 'D1', 'N114': 'D1 D12', 'N115': 'D1 D3', 'N116': 'D1 D3 D4', 'N117': 'D1 D11 D12',
    'N118': 'D1 D6', 'N119': 'D1 D6', 'N120': 'D3 D6', 'N121': 'D1 D6', 'N122': 'D1 D12', 'N123': 'D6', 'N124': 'D7 D12', 'N125': 'D1 D7 D12',
    'N126': 'D1 D3', 'N127': 'D1 D3 D12', 'N128': 'D3 D4', 'N129': 'D2', 'N130': 'D2', 'N131': 'D1 D7', 'N132': 'D7', 'N133': 'D7 D11', 'N134': 'D5 D7',
    'N135': 'D7 D12', 'N136': 'D1', 'N137': 'D4 D7 D12', 'N138': 'D3 D7', 'N139': 'D3 D4', 'N140': 'D1 D7', 'N141': 'D7', 'N142': 'D7 D8',
    'N143': 'D7 D8', 'N144': 'D8', 'N145': 'D1 D2', 'SU013': 'D1',
}


def load(name):
    with open(HERE + name, encoding='utf-8') as f:
        return json.load(f)


cases = {c['id']: c for c in load('use_cases.json')}
graded = {g['id']: g for g in load('out/results.graded.json')}
missing = [i for i in graded if i not in USES]
assert not missing and set(USES) <= set(graded), f'mapping mismatch: {missing}'

wb = Workbook()
head, bold = PatternFill('solid', fgColor='1F3864'), Font(bold=True, color='FFFFFF')
wrap = Alignment(wrap_text=True, vertical='top')

uc = wb.active
uc.title = 'Per use case'
cols = ['Use case', 'Name', 'Delivered? (run 4)', 'Answer quality (run 4)'] + list(ITEMS) + ['Items needed']
uc.append(cols)
n0 = 2
for r, (cid, needs) in enumerate(USES.items(), n0):
    g = graded[cid]
    uc.append([cid, cases.get(cid, {}).get('name', ''), g['delivery'], g['behaviour']] + ['x' if d in needs.split() else '' for d in ITEMS] + [None])
    uc.cell(r, len(cols), f'=COUNTIF(E{r}:{get_column_letter(4 + len(ITEMS))}{r},"x")')
last = n0 + len(USES) - 1

dn = wb.create_sheet('Data needed', 0)
dn.append(['ID', 'Data item', 'What exactly', 'Example fields', 'What the MES side has to do', 'Use cases it unlocks', 'Share of the 84', 'Priority rank'])
for r, (d, (name, what, fields, action)) in enumerate(ITEMS.items(), 2):
    col = get_column_letter(5 + list(ITEMS).index(d))
    dn.append([d, name, what, fields, action, f"=COUNTIF('Per use case'!{col}{n0}:{col}{last},\"x\")", f'=F{r}/COUNTA(\'Per use case\'!A{n0}:A{last})', f'=RANK(F{r},$F$2:$F${1 + len(ITEMS)})'])
    dn.cell(r, 7).number_format = '0%'
dn.append([])
dn.append(['', 'Use cases in this list', '', '', '', f"=COUNTA('Per use case'!A{n0}:A{last})"])

for ws, widths in ((dn, [6, 34, 44, 60, 46, 14, 12, 12]), (uc, [10, 38, 18, 18] + [5] * len(ITEMS) + [12])):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for c in ws[1]:
        c.fill, c.font, c.alignment = head, bold, Alignment(wrap_text=True, vertical='center')
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = wrap
    ws.freeze_panes = 'C2'
wb.save(sys.argv[1])
print('saved', sys.argv[1], 'use cases', len(USES))
