"""Rows of any table, optionally only those linked to one record, from the read-only database.

The Prisma schema (cableCORE_BE/prisma/models) gives every table, column and foreign key, so every module
(history, costing, carbon, current rating, tenders, requests, quotations, BOM...) is covered by the same
code. Names are checked against the schema and values are bound parameters; the connection is the
read-only `ai_reader` role.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from ai.apps.cablecore import records
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


async def rows(table: str, record: str = '', where: dict | None = None, columns: list[str] | None = None,
               order_by: str = '', limit: int = 100, offset: int = 0, via: str = '') -> dict:
    target = _model(table)
    conds, params, joins = [], [], {}
    link = ''
    if record:
        cond, link_params, link = _link(target, await _record_ids(record), via)
        conds.append(cond)
        params += link_params
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
    join_sql = ''.join(
        f' left join {_q(other.table)} {alias} on {alias}.{_q(other.columns[rel.refs[0]])} = t.{_q(target.columns[rel.fields[0]])}'
        for alias, other, rel in joins.values()
    )
    where_sql = (' where ' + ' and '.join(conds)) if conds else ''
    base = f'from {_q(target.table)} t{join_sql}{where_sql}'
    limit = min(max(int(limit), 1), MAX_LIMIT)
    total = (await asyncio.to_thread(records._rows, f'select count(*) as n {base}', tuple(params)))[0]['n']
    found = await asyncio.to_thread(records._rows, f'select {", ".join(select)} {base}{order} limit {limit} offset {int(offset)}', tuple(params))
    result = {'table': target.name, 'linked_by': link or None, 'total': total, 'offset': offset}
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
