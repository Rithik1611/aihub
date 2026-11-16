"""cableCORE helpers for sandbox scripts: `import cc`.

    api = cc.API(ticket)                     # read-only ticket from the cablecore_api_access tool
    api.rows('DesignHistory', record='LV-7035')        # table rows, same rules as the assistant's cc_related
    api.rows('LookupTable', where={'name': {'op': 'ilike', 'value': '%armour%'}}, all_pages=True)
    api.get('/account/profile')
    api.post('/os/designs/search', {'query': 'conductorCSA>=50', 'pageSize': 500})  # read-only POSTs only
    # Changes are never made from here: the assistant uses its cc_action tool.
    api.routes('catalogue')                  # API routes whose path / operationId / description match
    cc.code('validateDropdown', repo='cableCORE_BE')   # source search
"""

import base64
import json
import os
from typing import Any

import requests

API_BASE = os.environ['CC_API_BASE']
ROWS_URL = os.environ['CC_ROWS_URL']
_openapi: dict | None = None


class APIError(Exception):
    """cableCORE answered with an error status, or 200 with {success: false}."""

    def __init__(self, method: str, path: str, status: int, body: Any):
        self.status, self.body = status, body
        super().__init__(f'{method} {path} -> {status}: {json.dumps(body)[:1500] if not isinstance(body, str) else body[:1500]}')


class API:
    def __init__(self, ticket: str):
        self.headers = {'X-CC-Ticket': ticket}

    def _call(self, method: str, path: str, **kw) -> Any:
        r = requests.request(method, f'{API_BASE}/{path.lstrip("/")}', headers=self.headers, timeout=300, **kw)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        if r.status_code >= 400 or (isinstance(body, dict) and body.get('success') is False):
            raise APIError(method, path, r.status_code, body)
        return body

    def rows(self, table: str, all_pages: bool = False, **args: Any) -> dict:
        """Rows of a table (Prisma model name), all or those linked to `record`: {total, more, rows}. Same
        arguments and rules as the assistant's cc_related (record, where, columns, order_by, limit <= 500,
        offset, via, plant); the user's licence and plants apply. all_pages=True follows `more` to the end."""
        out: dict = {}
        while True:
            r = requests.post(ROWS_URL, headers=self.headers, json={'table': table, **args}, timeout=300)
            if r.status_code >= 400 or not r.text.startswith('{'):
                raise APIError('POST', f'rows {table}', r.status_code, r.text)
            page = r.json()
            out = {**page, 'rows': [*out.get('rows', []), *page.get('rows', [])]}
            if not (all_pages and page.get('more')):
                return out
            args['offset'] = len(out['rows'])

    def get(self, path: str, **params) -> Any:
        return self._call('GET', path, params=params)

    def post(self, path: str, body: Any = None, **params) -> Any:
        return self._call('POST', path, json=body, params=params)

    def download(self, path: str, to: str, **params) -> str:
        r = requests.get(f'{API_BASE}/{path.lstrip("/")}', headers=self.headers, params=params, timeout=300)
        if r.status_code >= 400:
            raise APIError('GET', path, r.status_code, r.text[:1500])
        with open(to, 'wb') as f:
            f.write(r.content)
        return to

    def _spec(self) -> dict:
        global _openapi
        if _openapi is None:
            _openapi = self.get('/openapi.json')
        return _openapi

    def capabilities(self, text: str = '') -> str:
        """The app's own actions (what its screens use), e.g. design.search: name, route, what it does."""
        out = []
        for path, ops in self._spec().get('paths', {}).items():
            for method, op in ops.items():
                name = op.get('operationId', '')
                if '.' in name and text.lower() in f"{name} {op.get('description', '')}".lower():
                    out.append(f"{name}: {method.upper()} {path}\n  {op.get('description', '').splitlines()[0][:300]}")
        return '\n'.join(sorted(out)) or f'no app action matches {text!r}'

    def routes(self, text: str, limit: int = 15) -> str:
        """Routes matching `text` (path, operationId or description) with parameters and body schema."""
        _openapi = self._spec()
        schemas = _openapi.get('components', {}).get('schemas', {})

        def resolve(node: Any, depth: int = 0) -> Any:
            if depth > 4:
                return '…'
            if isinstance(node, dict):
                if '$ref' in node:
                    return resolve(schemas.get(node['$ref'].split('/')[-1], {}), depth + 1)
                return {k: resolve(v, depth + 1) for k, v in node.items() if k not in ('example', 'examples')}
            if isinstance(node, list):
                return [resolve(n, depth + 1) for n in node]
            return node

        text = text.lower()
        out = []
        for path, ops in _openapi.get('paths', {}).items():
            for method, op in ops.items():
                hay = f"{path} {op.get('operationId', '')} {op.get('description', '')}".lower()
                if text in hay:
                    body = op.get('requestBody', {}).get('content', {})
                    schema = next(iter(body.values()), {}).get('schema') if body else None
                    out.append(
                        f"{method.upper()} {path}  [{op.get('operationId')}]\n"
                        + (f"  {op['description']}\n" if op.get('description') else '')
                        + (f"  params: {[(p['name'], p['in']) for p in op.get('parameters', [])]}\n" if op.get('parameters') else '')
                        + (f"  body: {json.dumps(resolve(schema))[:1200]}\n" if schema else '')
                    )
        return '\n'.join(out[:limit]) or f'no route matches {text!r}'


def code(query: str, repo: str | None = None, files: int = 8, context: int = 2) -> str:
    """Zoekt search over cableCORE source; repo is cableCORE_BE or cableCORE_FE."""
    q = f'repo:{repo} {query}' if repo else query
    r = requests.post(
        f"{os.environ['ZOEKT_URL']}/api/search",
        json={'Q': q, 'Opts': {'MaxDocDisplayCount': files, 'NumContextLines': context, 'ChunkMatches': True}},
        timeout=60,
    ).json()
    out = []
    for fm in (r.get('Result', {}).get('Files') or [])[:files]:
        out.append(f"## {fm['Repository']}/{fm['FileName']}")
        for cm in fm.get('ChunkMatches') or []:
            text = base64.b64decode(cm['Content']).decode(errors='replace')
            out.append(f"  L{cm['ContentStart']['LineNumber']}:\n" + '\n'.join('    ' + l for l in text.splitlines()))
    return '\n'.join(out) or f'no match for {q!r}'
