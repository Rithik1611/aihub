"""Rows of any table, optionally only those linked to one record, from the read-only database.

The Prisma schema (cableCORE_BE/prisma/models) gives every table, column and foreign key, so every module
(history, costing, carbon, current rating, tenders, requests, quotations, BOM...) is covered by the same
code. Names are checked against the schema and values are bound parameters; the connection is the
read-only `ai_reader` role.
"""

import asyncio
import functools
import heapq
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from ai.apps.cablecore import records
from ai.apps.cablecore.access import Plants
from ai.settings import settings

SCHEMA_DIR = settings().cc_schema_dir
MAX_LIMIT = 500
MAX_CHARS = 24000
OPS = {'=': '=', '!=': 'is distinct from', '>': '>', '>=': '>=', '<': '<', '<=': '<=', 'like': 'like', 'ilike': 'ilike',
       'in': 'in', 'not in': 'not in', 'is null': 'is null', 'not null': 'is not null'}


@dataclass
class Relation:
    name: str  # relation field, e.g. costingVersion
    fields: list[str]  # FK fields on this model
    target: str  # model the FK points to
    refs: list[str]  # referenced fields on the target


@dataclass
class Model:
    name: str
    table: str
    columns: dict[str, str] = field(default_factory=dict)  # field -> SQL column
    relations: list[Relation] = field(default_factory=list)


_models: dict[str, Model] | None = None


def models() -> dict[str, Model]:
    global _models
    if _models is None:
        blocks = {}
        for f in sorted(SCHEMA_DIR.glob('*.prisma')):
            for name, body in re.findall(r'^model\s+(\w+)\s*\{(.*?)^\}', f.read_text(encoding='utf-8'), re.M | re.S):
                blocks[name] = body
        parsed = {}
        for name, body in blocks.items():
            table = re.search(r'@@map\("(\w+)"\)', body)
            model = Model(name, table.group(1) if table else name)
            for line in body.splitlines():
                m = re.match(r'\s*(\w+)\s+(\w+)(\[\])?\??(.*)$', line)
                if not m or line.strip().startswith(('//', '@@')):
                    continue
                fname, ftype, is_list, rest = m.groups()
                rel = re.search(r'@relation\([^)]*fields:\s*\[([^\]]+)\][^)]*references:\s*\[([^\]]+)\]', rest)
                if rel:
                    model.relations.append(Relation(fname, [x.strip() for x in rel.group(1).split(',')], ftype,
                                                    [x.strip() for x in rel.group(2).split(',')]))
                elif ftype not in blocks and not is_list:
                    col = re.search(r'@map\("(\w+)"\)', rest)
                    model.columns[fname] = col.group(1) if col else fname
            parsed[name] = model
        _models = parsed
    return _models


def _model(name: str) -> Model:
    found = models().get(name) or next((m for m in models().values() if m.table == name or m.name.lower() == name.lower()), None)
    if not found:
        raise ValueError(f'No table {name!r}. Tables are the Prisma model names in the data map.')
    return found


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '') + '"'


# ---- plant scope: which plant a row belongs to, followed through foreign keys to Plant

# Records that own other rows: their plant is the row's plant (as the backend's PlantScopeService reads it, e.g. a
# design version's plant is its config's).
OWNERS = frozenset({'Design', 'DesignVersion', 'Config', 'EnquiryVersion', 'Enquiry'})
OWNER_HOP = 0.3  # three hops through owners still beat one through anything else
NAME_PENALTY = 3


def _own_plant(m: Model, alias: str) -> tuple[str, str] | None:
    """(condition, 'ids' | 'names') when the table says its plant itself; one %s, the plant ids or names."""
    if m.name == 'Plant':
        return f'{alias}.{_q(m.columns["id"])}::text = any(%s)', 'ids'
    if m.name == 'Design':  # a design has no plant column: its configs carry it
        cfg = models()['Config']
        return (f'{alias}.{_q(m.columns["id"])} in (select {_q(cfg.columns["designId"])} from {_q(cfg.table)} '
                f'where {_q(cfg.columns["plantId"])}::text = any(%s))'), 'ids'
    if 'plantId' in m.columns:  # no plant = shared by every plant
        col = f'{alias}.{_q(m.columns["plantId"])}'
        return f'({col}::text = any(%s) or {col} is null)', 'ids'
    for name in ('plant', 'plantName'):  # formulas and lookup tables store the plant's name ('ALL' = every plant)
        if name in m.columns:
            return f'{alias}.{_q(m.columns[name])} = any(%s)', 'names'
    return None


@functools.cache
def _plant_path(name: str) -> tuple[tuple[str, Relation], ...] | None:
    """The cheapest foreign-key route from a table to one that says its plant. A hop to the record that owns the
    row (design, design version, its config, enquiry) costs less than any other, and a table storing a plant
    *name* is a last resort ('ALL' rows there would let a row of any plant through)."""
    done = object()
    heap: list[tuple[float, int, str | object, tuple[tuple[str, Relation], ...]]] = [(0.0, 0, name, ())]
    seen: set[str] = set()
    tie = 1
    while heap:
        cost, _, current, path = heapq.heappop(heap)
        if current is done:
            return path
        if not isinstance(current, str) or current in seen:
            continue
        seen.add(current)
        model = models()[current]
        if own := _own_plant(model, 't'):
            heapq.heappush(heap, (cost + (NAME_PENALTY if own[1] == 'names' else 0), tie, done, path))
            tie += 1
            continue
        for rel in model.relations:
            if (len(rel.fields) == 1 and rel.target in models() and rel.target not in seen and rel.target != 'Account'
                    and rel.fields[0] in model.columns and rel.refs[0] in models()[rel.target].columns):
                heapq.heappush(heap, (cost + (OWNER_HOP if rel.target in OWNERS else 1), tie, rel.target, (*path, (current, rel))))
                tie += 1
    return None


def plant_condition(name: str, alias: str = 't') -> tuple[str, str] | None:
    """SQL keeping only rows of the given plants, with one %s (plant ids or names, as the second value says);
    None for a table no foreign key ties to a plant."""
    path = _plant_path(_model(name).name)
    if path is None:
        return None
    end = models()[path[-1][1].target] if path else _model(name)
    found = _own_plant(end, f'p{len(path)}' if path else alias)
    assert found  # _plant_path stops only at such a table
    cond, kind = found
    for i in range(len(path) - 1, -1, -1):
        source, rel = models()[path[i][0]], path[i][1]
        target = models()[rel.target]
        here, there = (alias if i == 0 else f'p{i}'), f'p{i + 1}'
        cond = (f'{here}.{_q(source.columns[rel.fields[0]])} in (select {there}.{_q(target.columns[rel.refs[0]])} '
                f'from {_q(target.table)} {there} where {cond})')
    return cond, kind


def has_plant(name: str) -> bool:
    return _plant_path(_model(name).name) is not None


async def _record_ids(record: str) -> list[tuple[str, list[str]]]:
    """(model, ids) the record stands for: a design/enquiry/catalogue code or id, or 'Model:field=value'."""
    generic = re.match(r'^(\w+):(\w+)=(.+)$', record)
    if generic:
        model, fname, value = _model(generic.group(1)), generic.group(2), generic.group(3).strip()
        if fname not in model.columns:
            raise ValueError(f'{model.name} has no field {fname!r}. Fields: {", ".join(model.columns)}')
        rows = await asyncio.to_thread(records._rows, f'select {_q("id")}::text as id from {_q(model.table)} where {_q(model.columns[fname])}::text = %s', (value,))
        return [(model.name, [r['id'] for r in rows])]
    found = await records.find_any(record)
    if not found:
        raise ValueError(f'No design, enquiry or catalogue matches {record!r}; use the form Model:field=value.')
    if 'catalogue_id' in found:
        return [('Catalogue', [found['catalogue_id']])]
    if 'enquiry_id' in found:
        return [('EnquiryVersion', [found['version_id']]), ('Enquiry', [found['enquiry_id']])]
    return [('DesignVersion', [found['version_id']]), ('Design', [found['design_id']])]


def _link(target: Model, owners: list[tuple[str, list[str]]], via: str) -> tuple[str, list[str], str]:
    """SQL condition tying target rows to the record, its parameters, and a readable description."""
    options = []
    for owner, ids in owners:
        for rel in target.relations:  # target -> owner (e.g. DesignHistory.designId -> DesignVersion)
            if rel.target == owner and len(rel.fields) == 1:
                options.append((rel.fields[0], f't.{_q(target.columns[rel.fields[0]])} = any(%s)', [ids], f'{target.name}.{rel.fields[0]} -> {owner}'))
        for rel in _model(owner).relations:  # owner -> target (e.g. Enquiry.customerId -> Customer)
            if rel.target == target.name and len(rel.fields) == 1:
                owner_m = _model(owner)
                cond = (f't.{_q(target.columns[rel.refs[0]])} in (select {_q(owner_m.columns[rel.fields[0]])} '
                        f'from {_q(owner_m.table)} where {_q("id")}::text = any(%s))')
                options.append((rel.name, cond, [ids], f'{owner}.{rel.fields[0]} -> {target.name}'))
        if options:
            break  # the most specific owner (e.g. the design version) wins
    if via:
        options = [o for o in options if o[0] == via]
    if not options:
        raise ValueError(f'{target.name} has no foreign key to {" or ".join(o for o, _ in owners)}' + (f' named {via!r}' if via else '')
                         + '. Choose a table that references the record, or list the table with `where` filters.')
    if len(options) > 1:
        raise ValueError(f'{target.name} links to the record in {len(options)} ways; pass via= one of: '
                         + ', '.join(f'{o[0]} ({o[3]})' for o in options))
    _, cond, params, text = options[0]
    return cond, params, text


def _column(target: Model, name: str, joins: dict[str, tuple[str, Model, Relation]]) -> str:
    """A filter/sort column: a field of the table, or relation.field one foreign key away (joined once)."""
    if name in target.columns:
        return f't.{_q(target.columns[name])}'
    if '.' in name:
        rel_name, fname = name.split('.', 1)
        rel = next((r for r in target.relations if r.name == rel_name or r.target == rel_name), None)
        if rel and len(rel.fields) == 1:
            other = _model(rel.target)
            if fname in other.columns:
                if rel.name not in joins:
                    joins[rel.name] = (f'j{len(joins)}', other, rel)
                return f'{joins[rel.name][0]}.{_q(other.columns[fname])}'
    raise ValueError(f'{target.name} has no field {name!r}. Fields: {", ".join(target.columns)}; '
                     f'related: {", ".join(r.name + "." for r in target.relations if r.target != "Account")}')


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (dict, list)):  # JSON columns (e.g. a lookup table's values) can be huge
        text = json.dumps(value, ensure_ascii=False, default=str)
        return value if len(text) <= 1500 else text[:1500] + f'… ({len(text)} chars; read this one record with fewer columns or in the sandbox)'
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + '…'
    return value


def model_name(table: str) -> str:
    """The Prisma model a table name stands for (raises ValueError for an unknown one)."""
    return _model(table).name


async def rows(table: str, record: str = '', where: dict | None = None, columns: list[str] | None = None,
               order_by: str = '', limit: int = 100, offset: int = 0, via: str = '',
               plants: Plants | None = None, refuse: Callable[[str], str | None] | None = None) -> dict:
    """plants: rows are limited to these plants, always (a record or filters only narrow it further); None = no
    limit (no access rules known). refuse(model) -> why a joined table may not be read, or None."""
    target = _model(table)
    conds: list[str] = []
    params: list[Any] = []
    joins: dict[str, tuple[str, Model, Relation]] = {}
    link = ''
    scope = None
    if record:
        cond, link_params, link = _link(target, await _record_ids(record), via)
        conds.append(cond)
        params += link_params
    if plants is not None and (found := plant_condition(target.name)):
        cond, kind = found
        conds.append(cond)
        params.append(plants.ids if kind == 'ids' else [*plants.names, 'ALL'])
        scope = plants.label
    for name, spec in (where or {}).items():
        op, value = (spec.get('op', '='), spec.get('value')) if isinstance(spec, dict) else ('=', spec)
        op = str(op).lower()
        if op not in OPS:
            raise ValueError(f'Unknown operator {op!r}; use one of {", ".join(OPS)}')
        col = _column(target, name, joins)
        if op in ('is null', 'not null'):
            conds.append(f'{col} {OPS[op]}')
        elif op in ('in', 'not in'):
            conds.append(f'{col}::text {"<> all" if op == "not in" else "= any"}(%s)')
            params.append([str(v) for v in (value if isinstance(value, list) else [value])])
        elif op in ('like', 'ilike'):
            conds.append(f'{col}::text {op} %s')
            params.append(str(value))
        else:
            conds.append(f'{col} {OPS[op]} %s')
            params.append(value)
    select = [_column(target, c, joins) + f' as {_q(c)}' for c in columns] if columns else ['t.*']
    order = ''
    if order_by:
        desc = order_by.startswith('-')
        order = f' order by {_column(target, order_by.lstrip("-"), joins)} {"desc" if desc else "asc"} nulls last'
    # ponytail: a joined row is checked for licence, not plant; it is the parent of an in-plant row
    for _alias, other, _rel in joins.values():
        if refuse and (why := refuse(other.name)):
            raise ValueError(f'{other.name}: {why}')
    join_sql = ''.join(
        f' left join {_q(other.table)} {alias} on {alias}.{_q(other.columns[rel.refs[0]])} = t.{_q(target.columns[rel.fields[0]])}'
        for alias, other, rel in joins.values()
    )
    where_sql = (' where ' + ' and '.join(conds)) if conds else ''
    base = f'from {_q(target.table)} t{join_sql}{where_sql}'
    limit = min(max(int(limit), 1), MAX_LIMIT)
    total = (await asyncio.to_thread(records._rows, f'select count(*) as n {base}', tuple(params)))[0]['n']
    found = await asyncio.to_thread(records._rows, f'select {", ".join(select)} {base}{order} limit {limit} offset {int(offset)}', tuple(params))
    result = {'table': target.name, 'linked_by': link or None, **({'plants': scope} if scope else {}), 'total': total, 'counted': f'{target.name} rows', 'offset': offset}
    return await asyncio.to_thread(_fit, result, found)  # CPU work stays off the event loop


def _fit(result: dict, found: list[dict]) -> dict:
    """Plain values, whole rows, and only as many rows as fit; always ends (fewer rows each pass, down to none)."""
    rows_out = [{k: _plain(v) for k, v in r.items() if v is not None} for r in found]
    result.update(returned=len(rows_out), more=result['total'] > result['offset'] + len(rows_out), rows=rows_out)
    while result['rows'] and len(json.dumps(result, ensure_ascii=False, default=str)) > MAX_CHARS:
        result['rows'] = result['rows'][: len(result['rows']) * 3 // 4]
        result['returned'], result['more'] = len(result['rows']), True
        result['note'] = 'Rows cut to fit: pass columns= for fewer fields, or offset= for the next rows.'
    return result


if __name__ == '__main__':
    m = models()
    assert m['Tender'].table == 'tc_tender' and m['Tender'].columns['externalId'] == 'external_id'
    assert any(r.name == 'costingVersion' and r.target == 'CostingVersion' for r in m['CostingVersionRollupBreakdown'].relations)
    assert 'designId' in m['DesignHistory'].columns
    print(f'related self-check OK ({len(m)} models)')
