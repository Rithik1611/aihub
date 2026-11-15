"""The cableCORE tools the model gets: search, records, related rows, formulas, every API route (find ->
describe -> action with approval), app notes, and the sandbox's read ticket."""

import asyncio
import difflib
import json
import uuid
from collections import Counter
from typing import Any, Literal

from ai import skills
from ai.apps.cablecore import access, api, attributes, formula, index, notes, records, related
from ai.apps.cablecore import actions as harness
from ai.apps.cablecore.actions import Action
from ai.apps.cablecore.prompt import SKILLS, fill
from ai.context import TurnContext
from ai.settings import settings
from ai.tools import Tool, tool


def _token(ctx: TurnContext) -> str:
    if not ctx.cc_token:
        raise api.CableCoreError('No cableCORE session for this user; they need to sign in again.')
    return ctx.cc_token


async def _access(ctx: TurnContext) -> access.Access | None:
    return await access.of(_token(ctx))


# What a search row is worth showing; the rest are internal ids, timestamps and flags.
_ROW_KEYS = {
    'design': ('designId', 'id', 'plant', 'productClassification', 'workflowStatus', 'version', 'Item Nature'),
    'enquiry': ('enquiryId', 'id', 'customerName', 'plant', 'status', 'priority', 'version'),
}
SEARCH_MAX_CHARS = 16_000
FIELDS_READ_ALL_MAX = 300  # with fields, every page is read when there are at most this many matches


def _compact_row(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    return {k: row[k] for k in _ROW_KEYS[kind] if row.get(k) not in (None, '')}


def _within(result: dict[str, Any], rows: list[dict[str, Any]], budget: int) -> str:
    """The result as JSON with as many whole rows as fit the budget: always valid JSON, never cut mid-row."""
    kept = len(rows)
    while True:
        text = json.dumps({**result, 'returned': kept, 'rows': rows[:kept]}, ensure_ascii=False, default=str)
        if len(text) <= budget or kept == 0:
            return text
        kept = max(0, kept * budget // len(text) - 1)  # shrink in proportion, then re-measure
        result = {**result, 'cut': f'{len(rows) - kept} more rows were read but not shown; narrow the query (add filters).'}


TABLE_PAGE = 150  # rows per page of a `fields` search
BY_VALUE_MAX = 40  # a field with more distinct values than this gets no per-value counts


def _table(result: dict[str, Any], rows: list[dict[str, Any]], fields: list[str], page: int) -> str:
    """Every match of a `fields` search as a compact table: columns once, values shared by every row once
    (`same_for_all`), counts per value of each field (`by_value`), and pages of TABLE_PAGE rows that fit."""
    rows = [{k: v for k, v in r.items() if k != 'id'} for r in rows]  # the code identifies a design
    columns = list(dict.fromkeys(k for r in rows for k in r))
    same = {c: rows[0].get(c) for c in columns if rows and len({json.dumps(r.get(c), default=str) for r in rows}) == 1}
    shown = [c for c in columns if c not in same]
    by_value = {}
    for f in fields:
        counts = Counter(str(r.get(f)) for r in rows if r.get(f) not in (None, ''))
        if 1 < len(counts) <= BY_VALUE_MAX:
            by_value[f] = dict(counts.most_common())
    pages = max(1, -(-len(rows) // TABLE_PAGE))
    page = min(max(page, 1), pages)
    chunk = [[r.get(c) for c in shown] for r in rows[(page - 1) * TABLE_PAGE: page * TABLE_PAGE]]
    complete = result.pop('complete', True)
    out = {**result, **({'same_for_all': same} if same else {}), **({'by_value': by_value} if by_value else {}),
           'columns': shown, 'page': page, 'pages': pages}
    while True:
        text = json.dumps({**out, 'rows': chunk, 'more': page < pages or not complete}, ensure_ascii=False, default=str)
        if len(text) <= SEARCH_MAX_CHARS or not chunk:
            return text
        chunk = chunk[: len(chunk) * 3 // 4]
        out['cut'] = f'Rows on this page cut to {len(chunk)} to fit; ask for fewer fields or add filters.'


def _filter_text(f: dict[str, Any]) -> str | None:
    """One parsed filter back in the query language, to count it on its own; None for forms not rebuilt."""
    category = {'plant': 'plant', 'bom': 'bom', 'cableHeader': 'cable'}.get(str(f.get('category')))
    field = (f'{f["processName"]}.{f["field"]}' if f.get('processName')
             else f'{category}.{f["field"]}' if f.get('type') == 'category-attribute' and category else f.get('field'))
    op, value = f.get('operator'), f.get('value')
    quoted = f'"{value}"' if isinstance(value, str) and (' ' in value or op == 'phrase') else value
    forms = {'exact': f'{field}={quoted}', 'phrase': f'{field}={quoted}', 'gte': f'{field}>={value}', 'lte': f'{field}<={value}',
             'gt': f'{field}>{value}', 'lt': f'{field}<{value}', 'contains': f'{field} contains {value}',
             'null': f'{field} is null', 'not_null': f'{field} is not null'}
    if op == 'in' and isinstance(value, list):
        return f'{field} in ({",".join(str(v) for v in value)})'
    if op == 'between' and isinstance(value, list) and len(value) == 2:
        return f'{field} between {value[0]} and {value[1]}'
    return forms.get(str(op))


async def _name_hint(f: dict[str, Any], token: str) -> str:
    """Why a design filter on its own matched nothing: a key the search index does not have, or a value no design has."""
    if f.get('type') == 'top-level':  # record fields (designId, …), not attributes
        return ''
    keys = await attributes.index_keys(token)
    field = str(f.get('field') or '').lower()
    key = f'{str(f["processName"]).lower()}.{field}' if f.get('processName') else field
    shown = f'plant.{field}' if f.get('category') == 'plant' else key
    if key in keys:  # the key is right, so no design has this value
        stored = ((await attributes.meta()).get(key) or {}).get('key', key)
        _count, common = await attributes.values(stored)
        return f'; most common values of {shown}: {", ".join(f"{v} ({n})" for v, n in common)}' if common else ''
    if f.get('processName'):
        processes = sorted({k.split('.', 1)[0] for k in keys if k.endswith(f'.{field}')})
        if processes:
            return f'; process {f["processName"]} has no {field} in the search index (processes that do: {", ".join(processes[:8])})'
    close = difflib.get_close_matches(key, list(keys), n=4, cutoff=0.6)
    return (f'; {key} is not a key in the search index' + (f' (similar: {", ".join(close)})' if close else '')
            + '; cc_attributes finds keys by meaning')


async def _why_empty(path: str, token: str, body: dict[str, Any], parsed: dict[str, Any] | None, kind: str) -> str:
    """For a search that matched nothing: how many each filter matches on its own, so the one to change is clear."""
    filters = [f for f in (parsed or {}).get('filters') or [] if isinstance(f, dict)]
    texts = [_filter_text(f) for f in filters]
    if len(filters) < 2 and not (filters and texts[0]):
        return 'No matches.'

    async def count(text: str) -> Any:
        status, data = await api.call('POST', path, token, body={**body, 'query': text, 'page': 1, 'pageSize': 1})
        return None if api.failed(status, data) else ((data or {}).get('pagination') or {}).get('total')

    counts = await asyncio.gather(*(count(t) for t in texts if t))
    lines, it = [], iter(counts)
    for f, text in zip(filters, texts):
        if text:
            n = next(it)
            hint = await _name_hint(f, token) if n == 0 and kind == 'design' else ''
            lines.append(f'{text} -> {n} alone{hint}')
    if len(lines) == 1:
        return f'No matches: {lines[0]}'
    return 'Nothing matches all filters together. Each filter on its own:\n' + '\n'.join(lines)


async def cc_search(ctx: TurnContext, kind: Literal['design', 'enquiry'], query: str, page: int = 1, page_size: int = 20, plant: str = '',
                    fields: list[str] | None = None) -> str:
    """
    Search the user's DesignCenter data the way the app's search screens do. Use this first for any
    question about their designs/cables or enquiries.
    Query language (one string, all parts ANDed), keys exactly as cc_attributes returns them:
      design attribute        plant.conductorType=CU   plant.armourMaterial="Round Wire"   plant.voltage="0.6/1"
      process attribute       LAYUP.noElements=3   STRANDING.conductorCSA=300
      comparisons / ranges    plant.conductorCSA>=50   plant.conductorCSA between 16 and 25
      lists / patterns        plant.conductorCSA in (95,120)   description like %XLPE%   designId contains LV
      missing values          plant.referenceStandard is null  (only designs that HAVE the attribute with no value)
      records                 designId=LV-7034   designId contains 7034   enquiryId=ENQ-0214   customerName="Cable_World"
    Only filters: free-text words are refused. A key without a prefix matches the attribute in ANY process
    (a template default in one process then pollutes the count): always use the prefixed key. Get keys from
    cc_attributes, never from memory; `description like %…%` only for a property with no attribute.
    Designs: `designs` is the number of distinct designs (the index holds every version of a design; rows
    are de-duplicated to the latest version). When nothing matches, the answer says how many each filter
    matches on its own, flags keys the index does not have, and lists a key's common values.

    :param kind: design or enquiry
    :param query: the search in the query language above
    :param page: result page (1-based)
    :param page_size: rows per page, max 20
    :param plant: empty = the user's active plant (the default for every question); a plant name when the user
        names one; "all" when they ask across all their plants. The answer says which plants were searched.
    :param fields: designs only: attribute values to add to every row, as keys (plant.voltage, plant.conductorCSA,
        INSULATING_MV.nomThickness). With fields and at most 300 matches, every match comes back in one table:
        `columns` + `rows`, values common to all rows in `same_for_all`, and counts per value in `by_value`
        (answer "how many of each" from it; no need to search value by value). `pages` > 1: ask for page=2...
    :return: designs (distinct), parsedQuery and compact rows (code, id, plant, status, and the fields asked for)
    """
    token = _token(ctx)
    path = {'design': '/os/designs/search', 'enquiry': '/os/enquiries/search'}.get(kind)
    if not path:
        return 'kind must be design or enquiry; for anything else use cc_find.'
    scope = 'every plant you can access'
    if allowed := await _access(ctx):
        if not allowed.allows(kind, 'core'):
            return access.NO_LICENCE
        try:
            plant = access.plant_name(allowed, plant)
        except ValueError as e:
            return str(e)
        scope = f'plant {plant}' if plant else 'all your plants'
    body = {'query': query, 'page': page, 'pageSize': min(max(page_size, 1), 20), **({'plant': plant} if plant else {})}
    status, data = await api.call('POST', path, token, body=body)
    if error := api.failed(status, data):
        return f'Search failed: {error}'
    rows = (data or {}).get('data') or []
    total = ((data or {}).get('pagination') or {}).get('total')
    parsed = (data or {}).get('parsedQuery')
    if words := (parsed or {}).get('textTerms'):
        return json.dumps({'refused': f'Free-text words are not allowed: {", ".join(map(str, words))}. Use filters only: '
                           'attribute keys from cc_attributes (key=value), designId=… / enquiryId=… for codes, '
                           'customerName="…" for customers; description like %…% only for a property with no attribute.',
                           'parsedQuery': parsed}, ensure_ascii=False)
    if total == 0:
        return json.dumps({'total': 0, 'searched': scope, 'parsedQuery': parsed, 'why': await _why_empty(path, token, body, parsed, kind)}, ensure_ascii=False)
    if kind != 'design':
        result: dict[str, Any] = {'total': total, 'searched': scope, 'parsedQuery': parsed, 'page': page,
                                  'more': isinstance(total, int) and total > (page - 1) * body['pageSize'] + len(rows)}
        return _within(result, [_compact_row(kind, r) for r in rows], SEARCH_MAX_CHARS)
    # Designs: the index holds every version of a design, so rows are read in bulk, de-duplicated to the latest
    # version, and counted as distinct designs; `designs` comes from the index's distinct-designId endpoint.
    rows, complete = await _design_rows(path, token, body, rows, total)
    unique = _latest_versions(rows)
    designs = len(unique) if complete else await _distinct_designs(token, query, plant)
    result = {'designs': designs, 'searched': scope, 'rows_in_index': total, 'parsedQuery': parsed}
    if note := await _null_note(parsed):
        result['note'] = note
    if fields:
        if not complete:
            result['note'] = f'{designs} designs: too many to compare in one call, showing the first {len(unique)}. Add filters.'
        refs = [str(r.get(k)) for r in unique for k in ('designId', 'id') if r.get(k)]
        wanted = {f: f.removeprefix('plant.') for f in fields}  # values_for takes design-level names unprefixed
        values = await asyncio.to_thread(records.values_for, refs, list(wanted.values()))
        for r in unique:
            ref = next((str(r[k]) for k in ('designId', 'id') if r.get(k) and str(r[k]) in values), None)
            r.update(values[ref] if ref else {})
        rows = [{**_compact_row(kind, r), **{f: r.get(n) for f, n in wanted.items()}} for r in unique]
        return _table({**result, 'complete': complete}, rows, list(wanted), page)
    size = body['pageSize']
    shown = unique[(page - 1) * size: page * size]
    result.update(page=page, more=len(unique) > page * size or not complete)
    return _within(result, [_compact_row(kind, r) for r in shown], SEARCH_MAX_CHARS)


async def _null_note(parsed: dict[str, Any] | None) -> str:
    """`key is null` matches only designs that have the attribute with an empty value, so say how many have it at all."""
    for f in (parsed or {}).get('filters') or []:
        if isinstance(f, dict) and f.get('operator') == 'null' and f.get('type') != 'top-level':
            field = str(f.get('field') or '')
            key = f"{f['processName']}.{field}" if f.get('processName') else field
            stored = ((await attributes.meta()).get(key.lower()) or {}).get('key', key)
            with_value = await asyncio.to_thread(records.designs_with_attribute, stored)
            designs = await asyncio.to_thread(records.design_count)
            return (f'"is null" counts only designs that have {stored} with an empty value. {with_value} of {designs} designs '
                    f'have a value for it; the other {designs - with_value} have no {stored} at all and are not counted here.')
    return ''


BULK_PAGE = 100
BULK_ROWS_MAX = 500  # versions read per search; beyond this the distinct count comes from the index


async def _design_rows(path: str, token: str, body: dict[str, Any], first: list[dict[str, Any]], total: Any) -> tuple[list[dict[str, Any]], bool]:
    """Rows of the search up to BULK_ROWS_MAX (pages of BULK_PAGE); complete = every row of the search was read."""
    if not isinstance(total, int) or total <= len(first):
        return first, True
    rows: list[dict[str, Any]] = []
    page = 1
    while len(rows) < min(total, BULK_ROWS_MAX):
        status, data = await api.call('POST', path, token, body={**body, 'page': page, 'pageSize': BULK_PAGE})
        if api.failed(status, data) or not (data or {}).get('data'):
            break
        rows += data['data']
        page += 1
    return rows, len(rows) >= total


def _latest_versions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per design code: its highest version, in the order designs first appear."""
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        code = str(r.get('designId') or r.get('id'))
        if code not in best or _version(r) > _version(best[code]):
            best[code] = r
    return list(best.values())


def _version(row: dict[str, Any]) -> float:
    try:
        return float(row.get('version') or 0)
    except (TypeError, ValueError):
        return 0.0


async def _distinct_designs(token: str, query: str, plant: str) -> int | None:
    """Distinct design codes matching the query, from the index (None when unavailable)."""
    status, data = await api.call('GET', '/os/designs/distinct/designId', token,
                                  query={'q': query, 'size': 10000, **({'plants': plant} if plant else {})})
    return None if api.failed(status, data) else (data or {}).get('total')

async def cc_attributes(ctx: TurnContext, words: list[str], process: str = '') -> str:
    """
    Find the search key for a design property before filtering on it, from the search index itself (not from
    memory): every attribute whose name or label contains one of the words, most used first, with every place it
    lives (`plant.<key>` at design level, `PROCESS.<key>` per process), how many designs have a value there, and
    the most common stored values. Give the words the app may use and your own synonyms: a property can be
    named differently from everyday terms (cores may be "elements" of a lay-up process). An attribute that lives
    in several places holds the same property for different product classes or plants: search each real key.
    A key flagged `warning` holds a template default on most designs: do not filter on it for a real property.
    Filter with the stored value form shown in `common_values` (CU not copper, 0.6/1 not 0.6/1 kV, 95 not 95 mm²).
    One call per property; call them all in the same step.

    :param words: words for the property, e.g. ["core", "element"], ["sheath", "jacket"], ["voltage"]
    :param process: only keys of this process, e.g. LAYUP
    :return: matching keys (use them as written in cc_search), most used first
    """
    total, rows = await attributes.find(_token(ctx), words, process)
    if not rows:
        return f'No key in the search index contains {", ".join(words)}. Try other words (synonyms, abbreviations).'
    return json.dumps({'matching_attributes': total, 'shown': len(rows), 'attributes': rows}, ensure_ascii=False)


async def cc_get(ctx: TurnContext, kind: Literal['design', 'enquiry', 'catalogue'], ref: str, process: str = '') -> str:
    """
    One record in full, as stored now: a design (header values, every process with its values, BOM,
    workflow state, versions), an enquiry (header, line items with their designs, files) or a catalogue
    (plant, formula mode, its designs).

    :param kind: design, enquiry or catalogue
    :param ref: design code (LV-7034), enquiry code (ENQ-0216), catalogue name, or an id
    :param process: designs only: just this process's values (e.g. INSULATION), for a design too big to show whole
    :return: the record as JSON
    """
    allowed = await _access(ctx)
    if allowed and not allowed.allows('enquiry' if kind == 'enquiry' else 'design', 'core'):
        return access.NO_LICENCE
    record = await records.get(kind, ref)
    if not record:
        return f'No {kind} matches {ref!r}. Search with cc_search.'
    if allowed and record.get('plant') and not allowed.plant(str(record['plant'])):
        return access.NOT_YOUR_PLANT.format(plant=record['plant'])
    if process:
        processes = record.get('processes') or []
        chosen = [p for p in processes if str(p.get('process', '')).lower() == process.lower()]
        if not chosen:
            return f'{ref} has no process {process!r}; its processes: {", ".join(str(p.get("process")) for p in processes)}'
        identity = {k: v for k, v in record.items() if k not in ('processes', 'bom', 'header')}
        return records.clip({**identity, 'processes': chosen})
    return records.clip(record)

async def cc_related(ctx: TurnContext, table: str, record: str = '', where: dict[str, Any] | None = None, columns: list[str] | None = None, order_by: str = '',
                     limit: int = 100, offset: int = 0, via: str = '', plant: str = '') -> str:
    """
    Rows of any table (the Prisma model names in the data map), all of them or only those linked to one
    record through the schema's foreign keys, with the total count. One call for things like a design's
    change history, cost roll-up, carbon breakdown or current rating calculations; an enquiry's lines; a
    customer's enquiries or quotations; tenders; design requests; lookup tables. Read-only. Tables with a plant
    are read for the user's active plant unless `plant` says otherwise; tables their licence does not cover are refused.
    Examples:
      table=DesignHistory, record=LV-7035, where={"createdAt": {"op": ">=", "value": "2026-01-01"}}
      table=CostingVersionRollupBreakdown, record=LV-7035, via=parentDesignId,
        where={"costingVersion.costingVersionName": "Q3", "level": 1}, order_by=-costPerLotSize
      table=Quotation, record="Customer:name=Acme Cables"
      table=LookupTable, where={"name": {"op": "ilike", "value": "%armour%"}}, columns=["name", "plant"]
    A record that links in several ways returns the choices for `via`. Check `total` and `more`: the
    answer is complete only when `more` is false.

    :param table: the table (Prisma model name, e.g. DesignHistory, Tender, LineItem)
    :param record: optional: a design/enquiry code, catalogue name, id, or Model:field=value
    :param where: optional filters {field: value} or {field: {"op": "=|!=|>|>=|<|<=|like|ilike|in|not in|is null|not null", "value": ...}}; a related model's field as relation.field
    :param columns: optional fields to return (fields or relation.field); default all
    :param order_by: optional field to sort by, "-field" for descending
    :param limit: rows to return, max 500
    :param offset: rows to skip, for the next page
    :param via: which foreign key links the table to the record, when there are several
    :param plant: empty = the active plant; a plant name the user names; "all" for all their plants
    :return: total, more, and the rows
    """
    allowed = await _access(ctx)
    try:
        plants = None
        if allowed:
            if why := access.table_refusal(allowed, related.model_name(table)):
                return why
            plants = access.plant_ids(allowed, plant)
        result = await related.rows(table, record, where, columns, order_by, limit, offset, via, plants)
        text = await asyncio.to_thread(json.dumps, result, ensure_ascii=False, default=str)  # off the event loop
    except ValueError as e:
        return str(e)
    return text

async def cc_formula(ctx: TurnContext, attribute: str, process: str = '', design: str = '', plant: str = '', classification: str = '',
                     focus: str = '', tables: list[str] | None = None, depth: int = 1) -> str:
    """
    The formula that computes an attribute, resolved the way the app resolves it, with what it depends on:
    the formulas of the attributes it reads (plant.X, layer.P.X, getLayerAttributeValue) and the lookup
    tables it calls. With `design`, the formula that design's stored value actually uses; otherwise the best
    match for plant and product classification (ALL as fallback). Use it to explain or audit a value and to
    check a formula against a standard's table.

    :param attribute: attribute name, e.g. nomThickness, noWires
    :param process: the process for a process attribute (e.g. SHEATHING); empty for a plant (header) attribute
    :param design: optional design code; its plant, classification and linked formula are used
    :param plant: plant name when no design is given; empty = the user's active plant
    :param classification: product classification name when no design is given
    :param focus: optional text, e.g. a standard's name: long formulas are cut to the branches mentioning it
    :param tables: lookup table names to return in full (others come back as name, rows and columns)
    :param depth: how many levels of dependencies to follow (1-3)
    :return: the formula, its dependencies' formulas, and the lookup tables
    """
    allowed = await _access(ctx)
    if allowed and not allowed.permits('readDesignFormula'):
        return access.NO_PERMISSION
    try:
        if allowed and not design:
            plant = access.plant_name(allowed, plant) or plant
        result = await formula.trace_async(attribute, process, design, plant, classification, depth, focus, tables or [])
    except ValueError as e:
        return str(e)
    return json.dumps(result, ensure_ascii=False, default=str)

async def cc_find(ctx: TurnContext, query: str, limit: int = 6, writes_only: bool = False) -> str:
    """
    Find the cableCORE API route for an operation, among every route the app has (create, copy, update,
    assign, import, export, workflow, catalogue, lookup tables, files, admin...). Describe the
    operation in plain words, e.g. "copy design", "add designs to catalogue", "assign design to line item".

    :param query: the operation in plain words
    :param limit: how many routes to list
    :param writes_only: only routes that change data
    :return: one line per route: id [risk] METHOD path | what it does | params | body fields
    """
    await index.ensure(_token(ctx))
    routes = index.find(query, min(max(limit, 1), 12), writes_only)
    allowed = await _access(ctx)
    lines = [index.line(r) + (f' | NOT ALLOWED for this user: {why}' if allowed and (why := access.route_refusal(allowed, r.method, r.access)) else '')
             for r in routes]
    return '\n'.join(lines) or f'No route matches {query!r}; try other words.'

async def cc_describe(ctx: TurnContext, id: str) -> str:
    """
    The exact input of one route (from cc_find or the catalogue): path, query and body fields with types,
    * marking required ones, enums, and whether it takes a file. Read it before cc_action on a route you
    have not used in this chat.

    :param id: the route id
    :return: the route's input shape
    """
    await index.ensure(_token(ctx))
    route = index.get(id)
    if not route:
        return f'No route {id!r}. Closest: ' + '; '.join(r.id for r in index.find(id.replace('_', ' ').replace('.', ' '), 4))
    allowed = await _access(ctx)
    if allowed and (why := access.route_refusal(allowed, route.method, route.access)):
        return f'NOT ALLOWED for this user: {why} Tell the user; do not look for another route to the same result.'
    return index.describe(route)

async def cc_action(ctx: TurnContext, actions: list[Action], verify: list[str] | None = None) -> str:
    """
    Run cableCORE API routes. Each action is {"id": route id, "path": {...}, "query": {...}, "body": {...},
    "file": "<sandbox path>" for uploads}. Arguments are checked against the route's schema first (all
    problems are listed at once and nothing runs). Changes are shown to the user as ONE approval card listing
    every change exactly, so put the actions of one request together in order; actions run in order and stop
    at the first failure. A later action can use an earlier one's result: a value "$1.data.versionId" is
    replaced by that field of action 1's result (so copy -> update the copy -> calculate is one call and one
    approval). Background jobs are followed until they finish. The result includes `after`: the affected
    designs/enquiries as stored now - base what you tell the user on `results` and `after`.

    :param actions: the actions to run, in order
    :param verify: design/enquiry codes or ids to read back afterwards, besides those found in the responses
    :return: per action status, result or error, job state; and `after` records
    """
    return await harness.run(actions, verify, ctx)

async def cc_note(ctx: TurnContext, fact: str, evidence: str) -> str:
    """
    Remember a fact about how cableCORE works that you verified in this chat and that later chats should
    not have to rediscover: a value format, which attribute holds what, how a route behaves, a working
    search form. Every later chat sees these notes. Only facts proven by a result in this chat.

    :param fact: the fact, one sentence
    :param evidence: what proved it (the route result, record, or source file)
    :return: whether it was noted
    """
    _token(ctx)
    return notes.add(fact, evidence)


async def cablecore_api_access(ctx: TurnContext, purpose: str) -> str:
    """
    A read-only ticket for sandbox scripts that read the cableCORE API in bulk (cc.API(ticket).get / .search,
    header X-CC-Ticket on $CC_API_BASE). It allows GET and the app's read-only POSTs such as searches;
    it lasts 15 minutes. Changes are never made from the sandbox: use cc_action.

    :param purpose: what the reads are for
    :return: JSON {ticket, expires_in_s}
    """
    if ctx.tickets is None:
        return json.dumps({'error': 'Sandbox API access is not available here.'})
    return json.dumps(await ctx.tickets.issue(ctx.user_id, ctx.chat_id))


async def cc_show_drawing(ctx: TurnContext, design_id: str) -> str:
    """
    Show a design's cable drawing (its cross-section) in the chat. Use it when the user asks to see a design's
    drawing, construction or what it looks like. The picture loads in the user's own browser with their own
    session, so only they can see it. The backend is asked first, so a drawing that cannot be made is reported
    instead of shown as a broken picture. Put the returned markdown line in your reply as it is.

    :param design_id: the design's id (the `id` from cc_search or cc_get; a UUID), not its code
    :return: the markdown image line to put in the reply, or the reason the drawing is not available
    """
    token = _token(ctx)
    try:
        uuid.UUID(design_id)
    except ValueError:
        return "design_id must be the design's id (a UUID from cc_search or cc_get), not its code."
    # The same request the browser will make, as the user: the drawing service may be down or not allowed, and a broken
    # picture tells the user nothing. (The backend renders it twice this way; a cache there would make the second one free.)
    status, data = await api.call('GET', f'/erp/{design_id}/drawingurl', token, timeout_s=60)
    if error := api.failed(status, data):
        return json.dumps({'error': f'The drawing could not be produced ({error[:240]}). Tell the user the drawing is not '
                                    'available right now and say why; do not show a picture.'})
    return f'![Design drawing]({settings().cc_public_url.rstrip("/")}/erp/{design_id}/drawingurl)'


def tools() -> list[Tool]:
    found = [skills.tool_search_tool()] if settings().tool_search else []
    return [tool(cc_search), tool(cc_attributes), tool(cc_get), tool(cc_related), tool(cc_formula), tool(cc_find), tool(cc_describe), tool(cc_show_drawing),
            tool(cc_action, sequential=True), tool(cc_note), tool(cablecore_api_access, sequential=True, defer=True),
            skills.load_skill_tool(SKILLS, fill), *found]
