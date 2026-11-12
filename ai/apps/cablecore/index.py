"""Every cableCORE route, searchable by intent, with its input schema and risk.

The model never sees the 1.5k routes: it asks `find` for the few that match, `describe` for one
route's input, and `validate` runs in the harness before a call goes out.
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from jsonschema import Draft7Validator
from rank_bm25 import BM25Okapi

from ai.apps.cablecore import api
from ai.settings import settings


@dataclass
class Route:
    id: str
    method: str
    path: str  # OpenAPI form: /design/{id}/copy
    summary: str
    risk: str  # read | write | destroy
    path_params: list[str] = field(default_factory=list)
    query_params: list[str] = field(default_factory=list)
    body_schema: dict | None = None
    file_field: str | None = None
    capability: dict | None = None  # the @AiCapability descriptor when the route has one


_routes: dict[str, Route] = {}
_bm25: BM25Okapi | None = None
_order: list[str] = []
_built_at = 0.0
_build_lock = asyncio.Lock()  # parallel tool calls may ask for the index at the same moment
# Rebuilt this often so new or changed backend routes show up without a restart.
INDEX_TTL_S = settings().cc_index_ttl_s
_TOKEN = re.compile(r'[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+')
_STOP = {'controller', 'the', 'a', 'an', 'of', 'to', 'for', 'and', 'or', 'by', 'in', 'on', 'with', 'id', 'get', 'v1', 'api'}


def _words(text: str) -> list[str]:
    return [w.lower() for w in _TOKEN.findall(text or '') if w.lower() not in _STOP]


def _resolve(node: Any, components: dict, depth: int = 0) -> Any:
    if depth > 6:
        return {}
    if isinstance(node, dict):
        if '$ref' in node:
            name = node['$ref'].split('/')[-1]
            return _resolve(components.get(name, {}), components, depth + 1)
        return {k: _resolve(v, components, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(n, components, depth + 1) for n in node]
    return node


def _risk(op: dict, method: str, capability: dict | None) -> str:
    if capability and capability.get('risk'):
        return capability['risk']
    if op.get('x-risk') in ('read', 'write', 'destroy'):  # handlers the backend marks read-only (AuditRead)
        return op['x-risk']
    tag = re.search(r'Risk:\s*(read|write|destroy)', op.get('description') or '')
    if tag:
        return tag.group(1)
    if method == 'GET':
        return 'read'
    return 'destroy' if method == 'DELETE' else 'write'


async def ensure(token: str) -> None:
    # ponytail: built with the first caller's token, so the catalogue is that user's view; per-role
    # index only if the backend's spec/capabilities ever differ by role.
    global _built_at
    async with _build_lock:
        if _bm25 is None or time.time() - _built_at > INDEX_TTL_S:
            try:
                await _build(token)
                _built_at = time.time()
            except api.CableCoreError:
                if _bm25 is None:
                    raise  # no index yet; an old one keeps serving when a refresh fails


_PATH_PARAM = re.compile(r'\{[^/]+\}')


async def is_read_route(method: str, path: str, token: str) -> bool:
    """For the sandbox gateway: GET, or a route the backend marks read-only (e.g. POST /os/designs/search)."""
    method = method.upper()
    if method == 'GET':
        return True
    await ensure(token)
    path = '/' + path.strip('/')
    return any(
        r.method == method and r.risk == 'read' and re.fullmatch(_PATH_PARAM.sub('[^/]+', r.path.rstrip('/')) + '/?', path)
        for r in _routes.values()
    )


async def _build(token: str) -> None:
    global _bm25, _order, _routes
    routes: dict[str, Route] = {}
    order: list[str] = []
    spec = await api.spec(token)
    caps = await api.capabilities(token)
    components = spec.get('components', {}).get('schemas', {})
    docs: list[list[str]] = []
    for path, ops in spec.get('paths', {}).items():
        for method, op in ops.items():
            method = method.upper()
            rid = op.get('operationId') or f'{method} {path}'
            cap = caps.get(rid)
            params = op.get('parameters') or []
            content = (op.get('requestBody') or {}).get('content') or {}
            multipart = next((v for k, v in content.items() if 'multipart' in k), None)
            body = None if multipart else _resolve(next(iter(content.values()), {}).get('schema'), components)
            file_field = None
            if multipart:
                props = _resolve(multipart.get('schema') or {}, components).get('properties', {})
                file_field = next((k for k, v in props.items() if v.get('format') == 'binary'), 'file')
            if cap and (cap.get('input') or {}).get('file'):
                file_field = cap['input']['file'].get('field') or file_field
            summary = (cap or {}).get('summary') or (op.get('description') or '').split('\n')[0] or ' '.join(_words(rid.split('_')[-1]))
            route = Route(
                id=rid,
                method=method,
                path=path,
                summary=summary,
                risk=_risk(op, method, cap),
                path_params=[p['name'] for p in params if p.get('in') == 'path'],
                query_params=[p['name'] for p in params if p.get('in') == 'query'],
                body_schema=body,
                file_field=file_field,
                capability=cap,
            )
            routes[rid] = route
            order.append(rid)
            fields = list((body or {}).get('properties', {}).keys())
            docs.append(_words(f"{rid} {rid.replace('.', ' ')} {path.replace('/', ' ')} {' '.join(fields)} {summary}"))
    _routes, _order, _bm25 = routes, order, BM25Okapi(docs)


def get(rid: str) -> Route | None:
    return _routes.get(rid)


def find(query: str, limit: int = 6, writes_only: bool = False) -> list[Route]:
    if _bm25 is None:
        return []
    scores = _bm25.get_scores(_words(query))
    # The described capabilities are the ones the app's screens are built on: prefer them on ties.
    ranked = sorted(range(len(_order)), key=lambda i: scores[i] + (0.5 if _routes[_order[i]].capability else 0), reverse=True)
    out = []
    for i in ranked:
        if scores[i] <= 0:
            break
        route = _routes[_order[i]]
        if writes_only and route.risk == 'read':
            continue
        out.append(route)
        if len(out) >= limit:
            break
    return out


def line(route: Route) -> str:
    fields = list((route.body_schema or {}).get('properties', {}).keys())[:8]
    parts = [f'{route.id} [{route.risk}] {route.method} {route.path}', route.summary[:200]]
    if route.path_params or route.query_params:
        parts.append('params: ' + ', '.join(route.path_params + [f'?{q}' for q in route.query_params[:6]]))
    if fields:
        parts.append('body: ' + ', '.join(fields))
    if route.file_field:
        parts.append(f'file: {route.file_field}')
    return ' | '.join(parts)


def _compact(schema: Any, defs: dict, depth: int = 0) -> Any:
    """A short readable shape: {"field": "string*", "items": [{...}]}; * marks required."""
    if not isinstance(schema, dict):
        return schema
    if '$ref' in schema:
        schema = defs.get(schema['$ref'].split('/')[-1], {})
    if depth > 3:
        return schema.get('type', 'object')
    kind = schema.get('type')
    if 'enum' in schema:
        return 'one of ' + json.dumps(schema['enum'])[:200]
    if kind == 'array' or 'items' in schema:
        return [_compact(schema.get('items', {}), defs, depth + 1)]
    if kind == 'object' or 'properties' in schema:
        required = set(schema.get('required') or [])
        return {
            k + ('*' if k in required else ''): _compact(v, defs, depth + 1)
            for k, v in (schema.get('properties') or {}).items()
        }
    for key in ('anyOf', 'oneOf', 'allOf'):
        if key in schema:
            return ' | '.join(str(_compact(s, defs, depth + 1)) for s in schema[key])[:200]
    out = kind or 'any'
    if schema.get('format'):
        out += f" ({schema['format']})"
    if schema.get('description'):
        out += f" — {schema['description'][:120]}"
    return out


def describe(route: Route) -> str:
    cap = route.capability
    lines = [line(route)]
    if cap:
        inp, defs = cap.get('input') or {}, cap.get('definitions') or {}
        for slot in ('path', 'query', 'body'):
            if inp.get(slot) and (inp[slot].get('properties') or inp[slot].get('$ref') or inp[slot].get('type') == 'array'):
                lines.append(f'{slot}: {json.dumps(_compact(inp[slot], defs), ensure_ascii=False)}')
        if inp.get('file'):
            lines.append(f"file: field '{inp['file'].get('field')}' (pass file=<sandbox path>)")
        if cap.get('returns'):
            lines.append(f"returns: {cap['returns']}")
        if cap.get('job'):
            lines.append('starts a background job; cc_action follows it to the end')
    else:
        if route.path_params:
            lines.append('path: ' + json.dumps({p + '*': 'string' for p in route.path_params}))
        if route.query_params:
            lines.append('query: ' + ', '.join(route.query_params))
        if route.body_schema:
            lines.append(f'body: {json.dumps(_compact(route.body_schema, {}), ensure_ascii=False)}')
        if route.file_field:
            lines.append(f"file: field '{route.file_field}' (pass file=<sandbox path>)")
    return '\n'.join(lines)[:6000]


def _any_of(node: Any) -> Any:
    # class-validator-jsonschema writes @IsDate as oneOf[date, date-time]; without format checks a
    # date matches both and oneOf rejects it.
    if isinstance(node, list):
        return [_any_of(n) for n in node]
    if isinstance(node, dict):
        return {('anyOf' if k == 'oneOf' else k): _any_of(v) for k, v in node.items()}
    return node


def _coerce(schema: dict | None, values: dict) -> dict:
    """Path and query values travel as text; turn "5000" into 5000 where the schema says number."""
    props = (schema or {}).get('properties') or {}
    out = dict(values)
    for key, value in values.items():
        kind = (props.get(key) or {}).get('type')
        if not isinstance(value, str):
            continue
        try:
            if kind == 'integer':
                out[key] = int(value)
            elif kind == 'number':
                out[key] = float(value)
            elif kind == 'boolean' and value.lower() in ('true', 'false'):
                out[key] = value.lower() == 'true'
        except ValueError:
            pass
    return out


def validate(route: Route, path: dict, query: dict, body: Any) -> list[str]:
    """Every problem at once, so one retry fixes the call. Strict for described routes."""
    errors = [f'path.{p}: required' for p in route.path_params if path.get(p) in (None, '')]
    cap = route.capability
    if cap:
        inp, defs = cap.get('input') or {}, cap.get('definitions') or {}
        for slot, value in (('query', _coerce(inp.get('query'), query)), ('body', body if body is not None else {})):
            schema = inp.get(slot)
            if not schema or (slot == 'body' and body is None and not schema.get('required')):
                continue
            for err in Draft7Validator(_any_of({**schema, 'definitions': defs})).iter_errors(value):
                where = '.'.join(str(p) for p in err.absolute_path)
                errors.append(f"{slot}{'.' + where if where else ''}: {err.message[:300]}")
    elif route.body_schema and isinstance(body, dict):
        for name in route.body_schema.get('required') or []:
            if name not in body:
                errors.append(f'body.{name}: required')
    elif route.body_schema and (route.body_schema.get('required') and body is None):
        errors.append('body: required')
    return errors


def fill_path(route: Route, path: dict) -> str:
    return re.sub(r'\{([^}]+)\}', lambda m: quote(str(path.get(m.group(1), '')), safe=''), route.path)


if __name__ == '__main__':
    assert _words('ProductClassificationsController_copyProductClassificationSamePlant')[:3] == ['product', 'classifications', 'copy']
    assert _compact({'type': 'object', 'required': ['a'], 'properties': {'a': {'type': 'string'}, 'b': {'type': 'array', 'items': {'type': 'integer'}}}}, {}) == {'a*': 'string', 'b': ['integer']}
    r = Route('x', 'POST', '/design/{id}/copy', 's', 'write', path_params=['id'], body_schema={'type': 'object', 'required': ['name'], 'properties': {'name': {'type': 'string'}}})
    assert fill_path(r, {'id': 'a b'}) == '/design/a%20b/copy'
    assert validate(r, {}, {}, {}) == ['path.id: required', 'body.name: required']
    print('index self-check OK')
