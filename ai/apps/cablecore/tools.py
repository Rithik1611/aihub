"""The cableCORE tools the model gets: search, records, related rows, formulas, every API route (find ->
describe -> action with approval), app notes, and the sandbox's read ticket."""

import asyncio
import difflib
import json
from typing import Any, Literal

from ai import skills
from ai.apps.cablecore import actions as harness
from ai.apps.cablecore import api, attributes, formula, index, notes, records, related
from ai.apps.cablecore.actions import Action
from ai.apps.cablecore.prompt import SKILLS, fill
from ai.context import TurnContext
from ai.settings import settings
from ai.tools import Tool, tool


def _token(ctx: TurnContext) -> str:
    if not ctx.cc_token:
        raise api.CableCoreError('No cableCORE session for this user; they need to sign in again.')
    return ctx.cc_token


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


def _filter_text(f: dict[str, Any]) -> str | None:
    """One parsed filter back in the query language, to count it on its own; None for forms not rebuilt."""
    field = f'{f["processName"]}.{f["field"]}' if f.get('processName') else f.get('field')
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
    if key in keys:  # the key is right, so no design has this value
        stored = ((await attributes.meta()).get(key) or {}).get('key', key)
        _count, common = await attributes.values(stored)
        return f'; most common values of {stored}: {", ".join(f"{v} ({n})" for v, n in common)}' if common else ''
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
    Query language (one string, all parts ANDed):
      attribute=value         conductorType=CU   armourMaterial="Round Wire"   voltage="0.6/1"
      comparisons / ranges    conductorCSA>=50   conductorCSA between 16 and 25
      lists / patterns        conductorCSA in (95,120)   description like %XLPE%   designId contains LV
      process attributes      STRANDING.conductorCSA=300
      missing values          referenceStandard is null / is not null
      free words              fuzzy text over codes and descriptions: LV-7034, 2XWY
    Filter on attribute keys from the search index: cc_attributes finds the key for a property (e.g. what holds
    the number of cores) with its stored values; do not guess names. Free words match loosely (a total in the
    thousands means they matched almost everything). When nothing matches, the answer says how many each
    filter matches on its own, flags keys the index does not have, and lists a key's common values.

    :param kind: design or enquiry
    :param query: the search in the query language above
    :param page: result page (1-based)
    :param page_size: rows per page, max 20
    :param plant: only when the user names a plant; empty searches every plant they can access
    :param fields: designs only: attribute values to add to every row, as header names (voltage, conductorCSA) or
        PROCESS.attribute (INSULATING_MV.nomThickness). With fields and at most 300 matches, all pages are read,
        so one call lists every match with the values to compare.
    :return: total, parsedQuery and compact rows (code, id, plant, status, and the fields asked for)
    """
    token = _token(ctx)
    path = {'design': '/os/designs/search', 'enquiry': '/os/enquiries/search'}.get(kind)
    if not path:
        return 'kind must be design or enquiry; for anything else use cc_find.'
    body = {'query': query, 'page': page, 'pageSize': min(max(page_size, 1), 20), **({'plant': plant} if plant else {})}
    status, data = await api.call('POST', path, token, body=body)
    if error := api.failed(status, data):
        return f'Search failed: {error}'
    rows = (data or {}).get('data') or []
    total = ((data or {}).get('pagination') or {}).get('total')
    parsed = (data or {}).get('parsedQuery')
    if total == 0:
        return json.dumps({'total': 0, 'parsedQuery': parsed, 'why': await _why_empty(path, token, body, parsed, kind)}, ensure_ascii=False)
    result: dict[str, Any] = {'total': total, 'parsedQuery': parsed}
    if fields and kind == 'design':
        read_all = isinstance(total, int) and total <= FIELDS_READ_ALL_MAX
        while isinstance(total, int) and read_all and len(rows) < total:
            body['page'] += 1
            status, more = await api.call('POST', path, token, body=body)
            if api.failed(status, more) or not (more or {}).get('data'):
                break
            rows += more['data']
        if not read_all:
            result['note'] = f'{total} matches: too many to compare, showing page {page}. Add filters to compare them all.'
        refs = [str(r.get(k)) for r in rows for k in ('designId', 'code', 'id', 'designVersionId') if r.get(k)]
        values = await asyncio.to_thread(records.values_for, refs, fields)
        for r in rows:
            ref = next((str(r[k]) for k in ('designId', 'code', 'id', 'designVersionId') if r.get(k) and str(r[k]) in values), None)
            r.update(values[ref] if ref else {})
        result['more'] = isinstance(total, int) and total > len(rows)
        return _within(result, [{**_compact_row(kind, r), **{f: r.get(f) for f in fields}} for r in rows], SEARCH_MAX_CHARS)
    # The answer is complete only on the last page.
    result.update(page=page, more=isinstance(total, int) and total > (page - 1) * body['pageSize'] + len(rows))
    return _within(result, [_compact_row(kind, r) for r in rows], SEARCH_MAX_CHARS)

async def cc_attributes(ctx: TurnContext, words: list[str], process: str = '') -> str:
    """
    Find the search key for a design property before filtering on it, from the search index itself (not from
    memory): every index key whose name or label contains one of the words, most used first, with its label,
    type, how many designs have a value, and the most common stored values. Give the words the app may use and
    your own synonyms: a property can be named differently from everyday terms (cores may be "elements" of a
    lay-up process), and the same property can exist on several processes (one per product class).

    :param words: words for the property, e.g. ["core", "element"], ["sheath", "jacket"], ["voltage"]
    :param process: only keys of this process, e.g. LAYUP
    :return: matching keys (use them as written in cc_search), most used first
    """
    total, rows = await attributes.find(_token(ctx), words, process)
    if not rows:
        return f'No key in the search index contains {", ".join(words)}. Try other words (synonyms, abbreviations).'
    return json.dumps({'matching_keys': total, 'shown': len(rows), 'keys': rows}, ensure_ascii=False)


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
    _token(ctx)  # a chat without a cableCORE session gets no data
    record = await records.get(kind, ref)
    if not record:
        return f'No {kind} matches {ref!r}. Search with cc_search.'
    if process:
        processes = record.get('processes') or []
        chosen = [p for p in processes if str(p.get('process', '')).lower() == process.lower()]
        if not chosen:
            return f'{ref} has no process {process!r}; its processes: {", ".join(str(p.get("process")) for p in processes)}'
        identity = {k: v for k, v in record.items() if k not in ('processes', 'bom', 'header')}
        return records.clip({**identity, 'processes': chosen})
    return records.clip(record)

async def cc_related(ctx: TurnContext, table: str, record: str = '', where: dict[str, Any] | None = None, columns: list[str] | None = None, order_by: str = '',
                     limit: int = 100, offset: int = 0, via: str = '') -> str:
    """
    Rows of any table (the Prisma model names in the data map), all of them or only those linked to one
    record through the schema's foreign keys, with the total count. One call for things like a design's
    change history, cost roll-up, carbon breakdown or current rating calculations; an enquiry's lines; a
    customer's enquiries or quotations; tenders; design requests; lookup tables. Read-only, across plants.
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
    :return: total, more, and the rows
    """
    _token(ctx)
    try:
        result = await related.rows(table, record, where, columns, order_by, limit, offset, via)
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
    :param plant: plant name when no design is given
    :param classification: product classification name when no design is given
    :param focus: optional text, e.g. a standard's name: long formulas are cut to the branches mentioning it
    :param tables: lookup table names to return in full (others come back as name, rows and columns)
    :param depth: how many levels of dependencies to follow (1-3)
    :return: the formula, its dependencies' formulas, and the lookup tables
    """
    _token(ctx)
    try:
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
    return '\n'.join(index.line(r) for r in routes) or f'No route matches {query!r}; try other words.'

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


def tools() -> list[Tool]:
    found = [skills.tool_search_tool()] if settings().tool_search else []
    return [tool(cc_search), tool(cc_attributes), tool(cc_get), tool(cc_related), tool(cc_formula), tool(cc_find), tool(cc_describe),
            tool(cc_action, sequential=True), tool(cc_note), tool(cablecore_api_access, sequential=True, defer=True),
            skills.load_skill_tool(SKILLS, fill), *found]
