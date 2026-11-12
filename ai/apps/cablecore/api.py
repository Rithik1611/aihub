"""HTTP to cableCORE as the user (their JWT, so the backend's own permissions apply)."""

import json
import re
from typing import Any

import aiohttp

from ai import http
from ai.settings import settings

CC_API_URL = settings().cc_api_url.rstrip('/')


class CableCoreError(Exception):
    pass


class FileResult:
    """A route answered with a file instead of JSON."""

    def __init__(self, name: str, content: bytes, mime: str):
        self.name, self.content, self.mime = name, content, mime

    def __repr__(self) -> str:
        return f'<file {self.name} {len(self.content)} bytes>'


async def call(
    method: str,
    path: str,
    token: str,
    *,
    query: dict | None = None,
    body: Any = None,
    file: tuple[str, bytes, str] | None = None,
    form: dict | None = None,
    file_field: str = 'file',
    timeout_s: int = 300,
) -> tuple[int, Any]:
    """Returns (status, parsed body). Never raises on HTTP errors: the caller decides."""
    headers = {'Authorization': f'Bearer {token}'}
    kwargs: dict[str, Any] = {'params': {k: v if isinstance(v, str) else json.dumps(v) for k, v in (query or {}).items() if v is not None}}
    if file:
        data = aiohttp.FormData()
        for key, value in (form or {}).items():
            data.add_field(key, value if isinstance(value, str) else json.dumps(value))
        name, content, mime = file
        data.add_field(file_field, content, filename=name, content_type=mime)
        kwargs['data'] = data
    elif body is not None:
        kwargs['json'] = body
    kwargs['timeout'] = aiohttp.ClientTimeout(total=timeout_s)
    async with http.session().request(method, f'{CC_API_URL}{path}', headers=headers, **kwargs) as r:
        raw = await r.read()
        kind = r.headers.get('content-type', '')
        disposition = r.headers.get('content-disposition', '')
        if r.status < 400 and raw and ('attachment' in disposition or not any(t in kind for t in ('json', 'text', 'xml'))):
            # A file (an Excel export, a PDF): kept as bytes for the caller to hand to the user.
            match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition)
            return r.status, FileResult(match.group(1) if match else 'download', raw, kind)
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = raw.decode(errors='replace')
        return r.status, data


def unwrap(data: Any) -> Any:
    """cableCORE wraps most answers as {statusCode, message, data}; the payload is `data`."""
    if isinstance(data, dict) and 'data' in data and ('statusCode' in data or 'success' in data or 'message' in data):
        return data['data']
    return data


def failed(status: int, data: Any) -> str | None:
    """The error to report, or None when the call succeeded (also a 200 answered with success: false)."""
    if status >= 400:
        if isinstance(data, dict):
            msg = data.get('message') or data.get('error') or data.get('detail')
            return f'{status}: {json.dumps(msg)[:1500] if not isinstance(msg, str) else msg[:1500]}'
        return f'{status}: {str(data)[:1500]}'
    if isinstance(data, dict) and data.get('success') is False:
        return f'{status} with success=false: {json.dumps(data.get("error") or data.get("message") or data)[:1500]}'
    return None


async def spec(token: str) -> dict:
    """The OpenAPI document. Cached by the index (index.ensure), not here."""
    status, data = await call('GET', '/openapi.json', token)
    if status != 200 or not isinstance(data, dict):
        raise CableCoreError('The cableCORE API reference is unavailable (is the backend running with OPENAPI_DOCS=true?).')
    return data


async def capabilities(token: str) -> dict[str, dict]:
    """The described actions (@AiCapability): accurate input schemas, risk, job and validate links."""
    status, data = await call('GET', '/ai/capabilities', token, query={'detail': 'true'})
    items = unwrap(data) if status == 200 else []
    return {c['name']: c for c in items or [] if isinstance(c, dict) and c.get('name')}
