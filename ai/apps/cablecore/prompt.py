"""The system prompt: prompt.md with the schema file index, the data map (foreign keys of the models users
ask about most) and the action catalogue (@AiCapability routes), plus per-turn values and the app notes."""

import re
import time

from ai.apps.cablecore import api, notes
from ai.apps.cablecore.related import SCHEMA_DIR
from ai.context import TurnContext
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
    return text
