"""The system prompt: prompt.md (the core: role, tools, rules, key facts), the app notes and the skills index.

Generated knowledge - the action catalogue (@AiCapability routes), the data map (foreign keys of the models users
ask about most) and the schema file index - lives in skills (skills/), loaded only when the model asks for it.
`fill` puts the generated parts into a skill's text when it is loaded.
"""

import re
import time
from pathlib import Path

from ai import skills as skill_lib
from ai.apps.cablecore import api, notes
from ai.apps.cablecore.related import SCHEMA_DIR
from ai.context import TurnContext
from ai.settings import settings

PROMPT_FILE = Path(__file__).parent / 'prompt.md'
SKILLS = skill_lib.discover(skill_lib.CORE_DIR, Path(__file__).parent / 'skills')
REBUILD_S = 1800  # same as the route index: new backend actions show up without a restart
TOOL_SEARCH_NOTE = ('- Sandbox and web tools are not listed until you need them: tool search finds and loads them '
                    '(e.g. run_command, read_file, write_file, search_web, fetch_url, cablecore_api_access).\n')

# The models users ask about most; their foreign keys are in the data-model skill, read from the schema.
MAP_MODELS = [
    'Design', 'Config', 'DesignVersion', 'PlantAttribute', 'PlantAttributeValue', 'CableHeaderAttributeValue',
    'ProcessDesign', 'ProcessAttribute', 'ProcessAttributeValue', 'BOMItem', 'DesignStatusFlow', 'WorkflowState',
    'Enquiry', 'EnquiryVersion', 'LineItem', 'EnquiryVersionFile', 'Catalogue', 'LookupTable', 'ProductClassification',
    'DesignHistory', 'DesignRequest', 'Quotation', 'QuotationVersion', 'QuotationLineItem', 'Customer',
    'CostingVersion', 'CostingVersionRollupBreakdown', 'CarbonRollupBreakdown', 'CurrentRatingCalculation',
    'Tender', 'TenderSpec', 'TenderLineItem',
]

_generated: tuple[float, dict[str, str]] = (0.0, {})


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


async def _generated_parts(token: str) -> dict[str, str]:
    global _generated
    if time.time() - _generated[0] > REBUILD_S:
        spec = await api.spec(token)
        _generated = (time.time(), {'{{SCHEMA_INDEX}}': schema_index(), '{{DATA_MAP}}': data_map(), '{{CATALOGUE}}': catalogue(spec)})
    return _generated[1]


async def fill(text: str, ctx: TurnContext) -> str:
    """A prompt or skill text with its placeholders filled for this user and turn."""
    if '{{SCHEMA_INDEX}}' in text or '{{DATA_MAP}}' in text or '{{CATALOGUE}}' in text:
        for key, value in (await _generated_parts(ctx.cc_token)).items():
            text = text.replace(key, value)
    return text.replace('{{USER_EMAIL}}', ctx.user_email).replace('{{CURRENT_DATE}}', time.strftime('%Y-%m-%d'))


async def instructions(ctx: TurnContext) -> str:
    text = PROMPT_FILE.read_text(encoding='utf-8')
    text = text.replace('{{SKILLS}}', skill_lib.index(SKILLS)).replace('{{TOOL_SEARCH}}', TOOL_SEARCH_NOTE if settings().tool_search else '')
    text = await fill(text, ctx)
    app_notes = notes.for_prompt()
    if app_notes:
        text = '# App notes (facts verified in earlier chats)\n' + app_notes + '\n\n' + text
    return text
