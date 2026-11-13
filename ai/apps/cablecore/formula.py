"""The formula behind an attribute, everything it depends on, and the lookup tables it reads.

Resolution is done here, the way the app picks formulas: the row for the attribute (and process) whose
productClassification matches best (weight 2), then plant (weight 1), `ALL` matching any. With a design,
the formula its stored value actually links to wins. Dependencies are read from the formula text:
`plant.X` (X's plant formula), `layer.this.X` / `layer.P.X` / getLayerAttributeValue(…, 'P', 'X') (process
formulas), and the table names passed to the lookup functions.
"""

import asyncio
import json
import re
from functools import lru_cache
from collections.abc import Sequence
from typing import Any

from ai.apps.cablecore import records

TABLES = {
    'process': ('FormulaProcessAttributes', 'formulaProcessAttributesId', 'ProcessAttributeValue', 'processAttributeId', 'ProcessAttribute'),
    'plant': ('FormulaPlantHeaders', 'formulaPlantHeadersId', 'PlantAttributeValue', 'plantAttributeId', 'PlantAttribute'),
}
LOOKUP_CALL = re.compile(r'cableCORE\.(\w*[Ll]ook[Uu]p\w*|getTableColumnContentByHeader|getColumnValuesByHeader|vlookupColumns)\(\s*([^,)]+)')
MAX_TABLE_ROWS = 80
MAX_FORMULA_CHARS = 6000
MAX_CHARS = 30000


def _q(sql: str, *params: Any) -> list[dict]:
    return records._rows(sql, params)


def _pick(rows: list[dict], plant: str, classification: str) -> dict | None:
    """Best match: classification (2) then plant (1). A row naming another plant/classification is out, unless
    that plant/classification was not given (then every plant's row is a candidate)."""
    def score(r: dict) -> int:
        c, p = r.get('productClassification') or 'ALL', r.get('plant') or 'ALL'
        if (classification != 'ALL' and c not in ('ALL', classification)) or (plant != 'ALL' and p not in ('ALL', plant)):
            return -1
        return (2 if c == classification and c != 'ALL' else 0) + (1 if p == plant and p != 'ALL' else 0)
    ranked = sorted((r for r in rows if score(r) >= 0), key=score, reverse=True)
    return ranked[0] if ranked else None


def _formula_rows(kind: str, attribute: str, process: str) -> list[dict]:
    table = TABLES[kind][0]
    if kind == 'process':
        return _q(f'select id::text, plant, "productClassification", "processName", "attributeName", formula from "{table}" '
                  'where "attributeName" = %s and "processName" = %s', attribute, process)
    return _q(f'select id::text, plant, "productClassification", "attributeName", formula from "{table}" where "attributeName" = %s', attribute)


def _linked_formula(kind: str, attribute: str, process: str, version_id: str) -> str | None:
    """The formula id the design's stored value links to, if any."""
    _, fk, values, attr_fk, attrs = TABLES[kind]
    extra = ' join "Process" pr on pr.id = a."processId" and pr.name = %s' if kind == 'process' else ''
    params = (process, attribute, version_id) if kind == 'process' else (attribute, version_id)
    rows = records._rows(f'select v."{fk}"::text as fid from "{values}" v join "{attrs}" a on a.id = v."{attr_fk}"{extra} '
                         f'where a.name = %s and v."designId"::text = %s and v."{fk}" is not null limit 1', params)
    return rows[0]['fid'] if rows else None


def _dependencies(formula: str, process: str) -> dict:
    plant = sorted(set(re.findall(r'\bplant\.(\w+)', formula)))
    layer = {('' if p == 'this' else p, a) for p, a in re.findall(r'\blayer\.(\w+)\.(\w+)', formula)}
    layer |= {(p, a) for p, a in re.findall(r"getLayerAttributeValues?\([^,]+,\s*['\"](\w+)['\"]\s*,\s*['\"](\w+)['\"]", formula)}
    lookups, dynamic = [], []
    for fn, arg in LOOKUP_CALL.findall(formula):
        arg = arg.strip()
        if arg[:1] in '\'"' and arg[-1:] == arg[:1]:
            lookups.append(arg[1:-1])
        else:
            dynamic.append(f'{fn}({arg}, …)')
    return {'plant': plant, 'layer': sorted((p or process, a) for p, a in layer), 'lookups': list(dict.fromkeys(lookups)), 'dynamic_lookups': dynamic}


def _lookup(name: str, plant: str, full: bool) -> dict:
    rows = _q('select name, plant, segment, "values" from "LookupTable" where name = %s', name)
    if not rows:
        return {'name': name, 'found': False}
    out = []
    for r in rows:
        values = r['values'] or []
        columns = list(values[0].keys()) if values else []
        table = {'plant': r['plant'], 'segment': r['segment'], 'rows': len(values)}
        if full:
            table.update(columns=columns, values=values[:MAX_TABLE_ROWS])
        else:
            table['columns'] = columns[:20] + ([f'… {len(columns) - 20} more'] if len(columns) > 20 else [])
        out.append(table)
    preferred = [t for t in out if t['plant'] in (plant, 'ALL')] or out
    return {'name': name, 'found': True, 'tables': preferred}


def _focus(formula: str, focus: str) -> str:
    """Only the parts of a long formula that mention `focus` (with their surrounding branch), plus its setup lines."""
    lines = formula.splitlines()
    hits = [i for i, line in enumerate(lines) if focus.lower() in line.lower()]
    if not focus or not hits:
        return formula
    keep = set(range(min(12, len(lines))))
    for i in hits:
        keep |= set(range(max(0, i - 3), min(len(lines), i + 30)))
    out, last = [], -1
    for i in sorted(keep):
        if i != last + 1:
            out.append(f'  … {i - last - 1} lines without {focus!r} omitted …')
        out.append(lines[i])
        last = i
    if last < len(lines) - 1:
        out.append(f'  … {len(lines) - 1 - last} lines without {focus!r} omitted …')
    return '\n'.join(out)


def _resolve(kind: str, attribute: str, process: str, plant: str, classification: str, version_id: str) -> dict:
    rows = _formula_rows(kind, attribute, process)
    linked = _linked_formula(kind, attribute, process, version_id) if version_id else None
    chosen = next((r for r in rows if r['id'] == linked), None) or _pick(rows, plant, classification)
    return {
        'attribute': f'{process}.{attribute}' if kind == 'process' else f'plant.{attribute}',
        'chosen_by': 'the design\'s stored value' if chosen and chosen['id'] == linked else 'best plant/classification match',
        'plant': chosen and chosen.get('plant'), 'classification': chosen and chosen.get('productClassification'),
        'other_rows': [f"{r.get('plant')}/{r.get('productClassification')}" for r in rows if not chosen or r['id'] != chosen['id']],
        'formula': (chosen['formula'] or '')[:MAX_FORMULA_CHARS] if chosen else None,
    }


def trace(attribute: str, process: str = '', design: str = '', plant: str = '', classification: str = '', depth: int = 1,
          focus: str = '', tables: list[str] | None = None) -> dict:
    result = _trace(attribute, process, design, plant, classification, depth, focus, tables or [])
    # Keep the answer usable: shorten dependency formulas, those without the focus text first.
    deps = sorted(result.get('dependencies', []), key=lambda d: bool(focus) and focus.lower() in (d.get('formula') or '').lower())
    for limit in (2500, 800, 0):
        for dep in deps:
            if len(json.dumps(result, default=str)) <= MAX_CHARS:
                return result
            if dep.get('formula') and len(dep['formula']) > limit:
                dep['formula'] = dep['formula'][:limit] + f'… (cut; cc_formula on {dep["attribute"]} for all of it)'
    return result


def _known(table: str, value: str, what: str) -> str:
    """A plant or product classification name as stored (case-insensitive); otherwise the valid names."""
    if not value or value == 'ALL':
        return 'ALL'
    names = [r['name'] for r in _q(f'select distinct name from "{table}" where name is not null order by name')]
    match = next((n for n in names if n.lower() == value.lower()), None)
    if not match:
        raise ValueError(f'No {what} named {value!r}. {what.capitalize()} names: {", ".join(names)}')
    return match


def _design_processes(version_id: str) -> list[tuple[str, float | None]]:
    rows = _q('select distinct pr.name from "ProcessDesign" pd join "Process" pr on pr.id = pd."processId" '
              'where pd."designId"::text = %s', version_id)
    return [(r['name'], None) for r in rows]


@lru_cache(maxsize=64)
def profile(classification: str, plant: str) -> tuple[tuple[str, float], ...]:
    """The processes designs of a classification (and plant) actually contain, with the share of designs."""
    rows = _q(
        'with d as (select dv.id from "DesignVersion" dv join "Config" c on c.id = dv."configId" join "Design" dd on dd.id = c."designId" '
        'join "ProductClassification" pc on pc.id = dd."productClassificationId" join "Plant" p on p.id = c."plantId" '
        'where pc.name = %s and (%s = \'ALL\' or p.name = %s)) '
        'select pr.name, count(distinct pd."designId")::float / greatest((select count(*) from d), 1) as share '
        'from d join "ProcessDesign" pd on pd."designId" = d.id join "Process" pr on pr.id = pd."processId" '
        'group by pr.name order by share desc', classification, plant, plant)
    return tuple((r['name'], round(r['share'], 2)) for r in rows if r['share'] >= 0.05)


def _with_formula(attribute: str, names: list[str]) -> set[str]:
    rows = _q('select distinct "processName" from "FormulaProcessAttributes" where "attributeName" = %s and "processName" = any(%s)', attribute, names)
    return {r['processName'] for r in rows}


def _check_process(requested: str, attribute: str, candidates: Sequence[tuple[str, float | None]], scope: str) -> str:
    """The process as this design / classification really has it. A name it does not have is refused with the
    processes it does have that compute `attribute`, so the caller picks the one that means what it needs
    (process names are product-specific: an MV design insulates in INSULATING_MV, not INSULATING)."""
    exact = next((n for n, _ in candidates if n.lower() == requested.lower()), None)
    if exact:
        return exact
    having = _with_formula(attribute, [n for n, _ in candidates])
    fitting = [f'{n}' + (f' (in {int(s * 100)}% of them)' if s else '') for n, s in candidates if n in having]
    others = [n for n, _ in candidates if n not in having]
    raise ValueError(
        f'{scope} have no process named {requested!r}. Their processes with a {attribute} formula: '
        f'{", ".join(fitting) or "none"}. Other processes: {", ".join(others[:30]) or "none"}. '
        'Call again with the process that means what you need.'
    )


def _trace(attribute: str, process: str, design: str, plant: str, classification: str, depth: int, focus: str, tables: list[str]) -> dict:
    version_id, extra = '', {}
    if design:
        found = records._design(design)
        if not found:
            raise ValueError(f'No design matches {design!r}.')
        version_id, plant, classification = found['version_id'], plant or found['plant'], classification or found['classification'] or 'ALL'
    plant = _known('Plant', plant, 'plant')
    classification = _known('ProductClassification', classification, 'product classification')
    if process:
        if version_id:
            candidates, scope = _design_processes(version_id), f'Design {design}'
        elif classification != 'ALL':
            candidates, scope = list(profile(classification, plant)), f'{classification} designs'
            extra['process_profile'] = [f'{n} {int(s * 100)}%' for n, s in candidates[:30]]
        else:
            candidates, scope = [(r['processName'], None) for r in _q('select distinct "processName" from "FormulaProcessAttributes" where "attributeName" = %s', attribute)], 'Designs'
        process = _check_process(process, attribute, candidates, scope)
    root = _resolve('process' if process else 'plant', attribute, process, plant, classification, version_id)
    root.update(extra)
    if not root['formula']:
        return {**root, 'note': 'No formula row for this attribute (and process); the value is entered by hand or imported.'}
    root['formula'] = _focus(root['formula'], focus)
    seen, frontier, deps, lookups = {root['attribute']}, [(root, process)], [], {}
    for _ in range(max(0, min(depth, 3))):
        nxt = []
        for node, proc in frontier:
            node['formula'] = _focus(node['formula'] or '', focus)
            found = _dependencies(node['formula'], proc)
            node['depends_on'] = found
            for name in found['lookups']:
                lookups.setdefault(name, None)
            for kind, p, a in [('plant', '', a) for a in found['plant']] + [('process', p, a) for p, a in found['layer']]:
                key = f'{p}.{a}' if kind == 'process' else f'plant.{a}'
                if key in seen:
                    continue
                seen.add(key)
                child = _resolve(kind, a, p, plant, classification, version_id)
                deps.append(child)
                if child['formula']:
                    nxt.append((child, p))
        frontier = nxt
    for name in list(lookups) + [t for t in tables if t not in lookups]:
        lookups[name] = _lookup(name, plant, name in tables)
    return {'design': design or None, 'plant': plant, 'classification': classification, 'formula': root,
            'dependencies': deps, 'lookup_tables': list(lookups.values())}


async def trace_async(*args: Any, **kwargs: Any) -> dict:
    return await asyncio.to_thread(trace, *args, **kwargs)


if __name__ == '__main__':
    deps = _dependencies("x = plant.voltage; y = layer.this.minAvgThickness; z = layer.LAYUP.noElements;"
                         " t = await cableCORE.vlookupByHeader('IEC 60502-2 Insulation Thickness', 'ALL', 'PROCESS', csa, v, 'ALL', true);"
                         " u = await cableCORE.e2keyVLookup(tableName, 'ALL');"
                         " w = await cableCORE.getLayerAttributeValue(cable.designId, 'STRANDING', 'conductorCSA')", 'INSULATING_MV')
    assert deps['plant'] == ['voltage'], deps
    assert ('INSULATING_MV', 'minAvgThickness') in deps['layer'] and ('LAYUP', 'noElements') in deps['layer'] and ('STRANDING', 'conductorCSA') in deps['layer'], deps
    assert deps['lookups'] == ['IEC 60502-2 Insulation Thickness'] and deps['dynamic_lookups'] == ['e2keyVLookup(tableName, …)'], deps
    assert _pick([{'plant': 'ALL', 'productClassification': 'ALL', 'id': 'a'}, {'plant': '1300', 'productClassification': 'ALL', 'id': 'b'},
                  {'plant': 'ALL', 'productClassification': 'MV', 'id': 'c'}, {'plant': 'X', 'productClassification': 'MV', 'id': 'd'}],
                 '1300', 'MV') == {'plant': 'ALL', 'productClassification': 'MV', 'id': 'c'}
    print('formula self-check OK')
