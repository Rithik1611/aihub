"""The system prompt: prompt.md with the schema file index, the data map (foreign keys of the models users
ask about most) and the action catalogue (@AiCapability routes), plus per-turn values and the app notes."""

import re
import time

from ai.apps.cablecore import api, notes
from ai.apps.cablecore.related import SCHEMA_DIR
from ai.context import TurnContext
from ai.settings import settings
from pathlib import Path

PROMPT_FILE = Path(__file__).parent / 'prompt.md'
REBUILD_S = 1800  # same as the route index: new backend actions show up without a restart

# The models users ask about most; their foreign keys are listed in the prompt, read from the schema.
MAP_MODELS = [
    'Design', 'Config', 'DesignVersion', 'PlantAttribute', 'PlantAttributeValue', 'CableHeaderAttributeValue',
    'ProcessDesign', 'ProcessAttribute', 'ProcessAttributeValue', 'BOMItem', 'DesignStatusFlow', 'WorkflowState',
    'Enquiry', 'EnquiryVersion', 'LineItem', 'EnquiryVersionFile', 'Catalogue', 'LookupTable', 'ProductClassification',
    'DesignHistory', 'DesignRequest', 'Quotation', 'QuotationVersion', 'QuotationLineItem', 'Customer',
    'CostingVersion', 'CostingVersionRollupBreakdown', 'CarbonRollupBreakdown', 'CurrentRatingCalculation',
    'Tender', 'TenderSpec', 'TenderLineItem',
]

_base: tuple[float, str] = (0.0, '')


def schema_index() -> str:
    lines = []
    for f in sorted(SCHEMA_DIR.glob('*.prisma')):
        text = f.read_text(encoding='utf-8')
        models = re.findall(r'^model\s+(\w+)', text, re.M)
        enums = re.findall(r'^enum\s+(\w+)', text, re.M)
        shown = ', '.join(models[:10]) + (f' +{len(models) - 10} more' if len(models) > 10 else '')
        lines.append(f'- `{f.name}`: {shown}' + (f' ({len(enums)} enums)' if enums else ''))
    return '\n'.join(lines)


def data_map() -> str:
    blocks = {}
    for f in SCHEMA_DIR.glob('*.prisma'):
        for name, body in re.findall(r'^model\s+(\w+)\s*\{(.*?)^\}', f.read_text(encoding='utf-8'), re.M | re.S):
            blocks[name] = body
    lines = []
    for name in MAP_MODELS:
        body = blocks.get(name)
        if body is None:
            continue
        fks = []
        for target, fields, refs in re.findall(
            r'^\s*\w+\s+(\w+)\??\s+@relation\([^)]*fields:\s*\[([^\]]+)\][^)]*references:\s*\[([^\]]+)\]', body, re.M
        ):
            if target != 'Account':  # created/modified-by audit links
                fks.append(f'{fields.strip()} → {target}.{refs.strip()}')
        table = re.search(r'@@map\("(\w+)"\)', body)
        label = f'`{name}` (SQL table `{table.group(1)}`)' if table else f'`{name}`'
        lines.append(f'- {label}: ' + ('; '.join(fks) if fks else '(no foreign keys)'))
    return '\n'.join(lines)


def catalogue(spec: dict) -> str:
    """The app's described actions (@AiCapability) from the OpenAPI document: id [risk] - first sentence."""
    lines = []
    for ops in spec.get('paths', {}).values():
        for op in ops.values():
            name = op.get('operationId', '')
            if '.' not in name:
                continue
            desc = op.get('description') or ''
            risk = (re.search(r'Risk:\s*(\w+)', desc) or [None, '?'])[1]
            first = re.split(r'(?<=[.:])\s', desc.split('\n')[0])[0][:110]
            lines.append(f'- {name} [{risk}] — {first}')
    return '\n'.join(sorted(lines))


async def _base_prompt(token: str) -> str:
    global _base
    if time.time() - _base[0] > REBUILD_S:
        spec = await api.spec(token)
        text = (PROMPT_FILE.read_text(encoding='utf-8')
                .replace('{{SCHEMA_INDEX}}', schema_index())
                .replace('{{DATA_MAP}}', data_map())
                .replace('{{CATALOGUE}}', catalogue(spec)))
        _base = (time.time(), text)
    return _base[1]


async def instructions(ctx: TurnContext) -> str:
    text = (await _base_prompt(ctx.cc_token)).replace('{{USER_EMAIL}}', ctx.user_email).replace('{{CURRENT_DATE}}', time.strftime('%Y-%m-%d'))
    app_notes = notes.for_prompt()
    if app_notes:
        text = '# App notes (facts verified in earlier chats)\n' + app_notes + '\n\n' + text
    return for_mode(text, settings().cc_tool_mode)


# ---- CC_TOOL_MODE variants: only the lines about reaching the API (and, for codemode_all, the data
# tools) change; the harness prompt is prompt.md as is.

_CODE_API = (
    '- `cc_code` — any operation the app has (1.5k routes: create, copy, update attributes, add processes, assign '
    'to line items, catalogues, lookup tables, imports, exports, workflow…). Write ONE JavaScript function: find '
    'routes with `codemode.search("2-4 words")`, read a route\'s exact input with `codemode.describe(path)`, call it '
    'as `cablecore.<method>(...)`; loops, filters and calculations go in the same code, so one call does a whole '
    'multi-step task. The catalogue below lists the main actions by id (method names replace "." with "_").'
)
_CODE_CHANGES = [
    ('- Every change goes through `cc_action`.',
     '- Every change goes through `cc_code`. A call that changes data pauses the run and the user gets an approval '
     'card listing that exact call, so do not ask for approval in your reply: put the whole request in one function, '
     'in order (copy, then update the copy using the id from the first result). Describe a route you have not used in '
     'this chat before calling it. Attribute updates take attribute names (`{"name": "conductorCSA", "value": "240"}`).'),
    ('- `cc_action` returns each action',
     '- `cc_code` returns `status`, your function\'s `result` and the API calls made. After a change, read the affected '
     'records back in the same function and return the stored values; report from that result only. If a value you set '
     'is not in it, say so. A reply that changed data is checked automatically against what the calls did.'),
    ('- When an action fails',
     '- When the run fails, read the error and logs; check `codemode.describe`, the app notes, or the source '
     '(`cc.code`), fix the code and run it again.'),
]
_DATA_IN_CODE = (
    '- Data reads run inside `cc_code` as `dc.search({kind, query, fields})` (the app\'s search with its query '
    'language: `conductorCSA>=50`, `armourMaterial="Round Wire"`, `STRANDING.conductorCSA=300`; with `fields` every '
    'match comes back with those values), `dc.get({kind, ref})` (one design/enquiry/catalogue in full), '
    '`dc.related({table, record, where, columns, order_by, limit})` (rows of any table in the data map, linked to a '
    'record) and `dc.formula({attribute, process, design})` (the formula behind an attribute). Read and compare many '
    'records in one function instead of one call each; check `total` and `more`.'
)
_DATA_TOOLS = ('`cc_search`', '`cc_get`', '`cc_related`', '`cc_formula`')


def _replace_line(lines: list[str], prefix: str, new: str | None) -> None:
    hits = [i for i, line in enumerate(lines) if line.startswith(prefix)]
    if len(hits) != 1:
        raise RuntimeError(f'prompt.md changed: expected one line starting {prefix!r} for CC_TOOL_MODE (found {len(hits)})')
    if new is None:
        del lines[hits[0]]
    else:
        lines[hits[0]] = new


def for_mode(text: str, mode: str) -> str:
    if mode == 'harness':
        return text
    lines = text.split('\n')
    _replace_line(lines, '- `cc_find` → `cc_describe` → `cc_action`', _CODE_API)
    for prefix, new in _CODE_CHANGES:
        _replace_line(lines, prefix, new)
    if mode == 'codemode_all':
        _replace_line(lines, '- `cc_search`', _DATA_IN_CODE)
        for name in _DATA_TOOLS[1:]:
            _replace_line(lines, f'- {name}', None)
    text = '\n'.join(lines).replace('comes back from `cc_action` with a `download_link`', 'comes back from your API call with a `download_link`')
    if mode == 'codemode_all':
        for name in _DATA_TOOLS:
            text = text.replace(name, f'`dc.{name[4:-1]}`')
    return text
