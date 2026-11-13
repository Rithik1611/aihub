"""The sandbox (Open Terminal): its tools for the model (run_command, files...), and file transfer.

The backend uses read_file / save_file for downloads and attachments (save_file(..., folder='uploads')
then context.user_message(text, attachment=(name, path))).
"""

import json
import os
import re
import time
import uuid
from typing import Any

import aiohttp

from ai import http
from ai.context import TurnContext
from ai.settings import settings
from ai.tools import Tool

SANDBOX_URL = settings().cc_terminal_url.rstrip('/')
# The sandbox functions the model gets (Open Terminal has more: browser, desktop, notebooks...).
TOOL_NAMES = settings().cc_sandbox_tools.split(',')
SPEC_TTL_S = 600


class SandboxError(Exception):
    pass


def _headers() -> dict:
    return {'Authorization': f'Bearer {settings().open_terminal_api_key.get_secret_value()}'}


async def read_file(path: str) -> tuple[str, bytes, str]:
    """-> (file name, content, mime)"""
    async with http.session().get(f'{SANDBOX_URL}/files/view', params={'path': path}, headers=_headers()) as r:
        if r.status != 200:
            raise SandboxError(f'Sandbox file {path} could not be read ({r.status}).')
        return os.path.basename(path), await r.read(), r.headers.get('content-type', 'application/octet-stream')


async def save_file(name: str, content: bytes, mime: str, folder: str = 'exports') -> str:
    """Stores a file under /home/user/work/<folder>/<random>/ and returns its sandbox path."""
    directory = f'/home/user/work/{folder}/{uuid.uuid4().hex[:8]}'
    form = aiohttp.FormData()
    form.add_field('file', content, filename=os.path.basename(name) or 'download', content_type=mime)
    async with http.session().post(f'{SANDBOX_URL}/files/upload', params={'directory': directory}, data=form, headers=_headers()) as r:
        if r.status != 200:
            raise SandboxError(f'The returned file could not be stored ({r.status}).')
        return (await r.json())['path']


# ---- the model's sandbox tools, built from the sandbox's own OpenAPI

_spec: tuple[float, dict] = (0.0, {})


async def _get(path: str) -> dict:
    async with http.session().get(f'{SANDBOX_URL}{path}', headers=_headers(), timeout=aiohttp.ClientTimeout(total=10)) as r:
        return await r.json() if r.status == 200 else {}


async def _openapi() -> dict:
    global _spec
    if time.time() - _spec[0] > SPEC_TTL_S:
        _spec = (time.time(), await _get('/openapi.json'))
    return _spec[1]


def _resolve(node: Any, components: dict[str, Any], depth: int = 0) -> Any:
    if depth > 8:
        return {}
    if isinstance(node, dict):
        if '$ref' in node:
            return _resolve(components.get(node['$ref'].split('/')[-1], {}), components, depth + 1)
        return {k: _resolve(v, components, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(n, components, depth + 1) for n in node]
    return node


def _make(method: str, path: str, op: dict, components: dict, cwd: str) -> Tool:
    params = [p for p in op.get('parameters') or [] if p.get('in') in ('path', 'query')]
    body = _resolve(((op.get('requestBody') or {}).get('content') or {}).get('application/json', {}).get('schema') or {}, components)
    properties = {p['name']: {**_resolve(p.get('schema') or {}, components), **({'description': p['description']} if p.get('description') else {})}
                  for p in params}
    properties.update(body.get('properties') or {})
    required = [p['name'] for p in params if p.get('required')] + list(body.get('required') or [])
    description = op.get('description') or op.get('summary') or ''
    if op['operationId'] == 'run_command' and cwd:
        description += f'\n\nThe current working directory is: {cwd}'
    in_path = {p['name'] for p in params if p['in'] == 'path'}
    in_query = {p['name'] for p in params if p['in'] == 'query'}

    async def call(ctx: TurnContext, **args) -> str:
        url = SANDBOX_URL + re.sub(r'\{(\w+)\}', lambda m: str(args.get(m.group(1), '')), path)
        query = {k: v if isinstance(v, str) else json.dumps(v) for k, v in args.items() if k in in_query and v is not None}
        payload = {k: v for k, v in args.items() if k not in in_path | in_query}
        headers = {**_headers(), 'X-User-Id': ctx.user_id, 'X-Session-Id': ctx.chat_id}
        async with http.session().request(method, url, params=query, json=payload if body else None, headers=headers,
                                          timeout=aiohttp.ClientTimeout(total=330)) as r:
            text = await r.text(errors='replace')
            return json.dumps({'error': f'{r.status}: {text[:2000]}'}) if r.status >= 400 else text

    return Tool(op['operationId'], description, {'type': 'object', 'properties': properties, 'required': required}, call,
                defer=True)  # found by tool search when it is on


async def tools(ctx: TurnContext) -> list[Tool]:
    """The sandbox functions in TOOL_NAMES; none when the sandbox is down (the chat still works without it)."""
    try:
        spec, cwd = await _openapi(), (await _get('/files/cwd')).get('cwd', '')
    except Exception:
        return []
    components = (spec.get('components') or {}).get('schemas') or {}
    return [_make(method.upper(), path, op, components, cwd)
            for path, ops in (spec.get('paths') or {}).items() for method, op in ops.items()
            if isinstance(op, dict) and op.get('operationId') in TOOL_NAMES]


async def instructions(ctx: TurnContext) -> str:
    """The sandbox's own system prompt (machine, user, how to work), added to the app's."""
    try:
        if (await _get('/api/config')).get('features', {}).get('system'):
            prompt = (await _get('/system')).get('prompt')
            return f'\n\n{prompt}' if prompt else ''
    except Exception:
        pass
    return ''
