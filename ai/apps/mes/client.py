"""The MES (Mendix) MCP server: its tools for the model, one token per user (MES_USER_TOKENS).

tools(ctx) builds the model's tools from the server's own tools/list, cached per user; none when the user has no
token or the server is down (the chat still works without them). The MCP side is just JSON-RPC over HTTP (streamable
HTTP: a session id, answers as SSE or JSON), so it reuses http.session() and adds no dependency.

Guards, because the server's tool list goes straight to the model:
- only read tools: a tool must say readOnlyHint, or be on MES_READ_TOOLS; any other is dropped (a write tool the
  server adds later never reaches the model without review),
- descriptions are capped and schemas cut down to what a function schema accepts, so one odd tool cannot break
  the whole request,
- a turn never waits on a slow MES: a known list is used at once and refreshed in the background; a failure is
  remembered for a minute, and connecting gives up after 3 s.
"""

import asyncio
import contextlib
import itertools
import json
import logging
import re
import time
from typing import Any

import aiohttp

from ai import http
from ai.context import TurnContext
from ai.settings import settings
from ai.tools import Tool

PROTOCOL = '2025-06-18'
TTL_S = 600  # tools/list is refreshed after this (in the background once known)
FAILED_S = 60  # after a failure, no MES tools (and no MES wait) for this long
CACHE_MAX = 256  # users kept per cache; the oldest goes first, so memory cannot grow without bound
DESCRIPTION_MAX = 600
TIMEOUT = aiohttp.ClientTimeout(total=30, sock_connect=3)
logger = logging.getLogger('aihub.mes')  # tool, user id, time, outcome: never the token or the data
_sessions: dict[str, tuple[float, str]] = {}  # token -> (when, MCP session id)
_specs: dict[str, tuple[float, list[dict[str, Any]]]] = {}  # token -> (when, tools/list)
_failed: dict[str, float] = {}  # token -> when tools/list last failed
_refreshing: set[str] = set()
_background: set[asyncio.Task[None]] = set()


def _keep(cache: dict[str, Any], key: str, value: Any) -> None:
    if key not in cache and len(cache) >= CACHE_MAX:
        cache.pop(next(iter(cache)))
    cache[key] = value


class MesError(Exception):
    pass


def _breaks(value: Any) -> Any:
    """The MES joins the lines of its text answers with the pilcrow sign; give the model real line breaks."""
    if isinstance(value, str):
        return re.sub(r'[ \t]*¶[ \t]*', '\n', value)  # and the spaces around it, so every line starts at the margin
    if isinstance(value, dict):
        return {k: _breaks(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_breaks(v) for v in value]
    return value


def clean(text: str) -> str:
    """The MES wraps every answer as {"tool": ..., "data": {...}}. Hand the model just the data: the text itself when the answer is
    text, labelled blocks when there are several texts, compact JSON when it is structured (lists, counts)."""
    try:
        payload = json.loads(text)
    except ValueError:
        return _breaks(text)
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, (dict, list)) or not data:
        return _breaks(text)
    data = _breaks(data)
    if isinstance(data, dict) and all(isinstance(v, str) for v in data.values()):
        return '\n\n'.join(next(iter(data.values())) if len(data) == 1 else f'{k}:\n{v}' for k, v in data.items())
    return json.dumps(data, ensure_ascii=False)


def token(ctx: TurnContext) -> str:
    """The user's MES token, by cableCORE username (ctx.user_email holds the username)."""
    # ponytail: tokens come from env JSON {"username": "token"}; a per-user table (encrypted) when users self-serve
    raw = settings().mes_user_tokens.get_secret_value()
    if not raw or not settings().mes_mcp_url:
        return ''
    try:
        tokens = json.loads(raw)
    except ValueError:  # e.g. a shell that sourced the .env dropped the JSON's quotes: quote the value ('{...}')
        logger.warning('MES_USER_TOKENS is not valid JSON: MES is off')
        return ''
    return {str(k).lower(): v for k, v in tokens.items()}.get(ctx.user_email.lower(), '') if isinstance(tokens, dict) else ''


def _message(text: str) -> dict[str, Any] | None:
    for line in text.splitlines():  # SSE: `data: {json-rpc}`
        if line.startswith('data:'):
            return json.loads(line[5:])
    try:
        return json.loads(text)
    except ValueError:
        return None


async def _post(token: str, body: dict[str, Any], sid: str = '') -> tuple[int, str, dict[str, Any] | None]:
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': PROTOCOL}
    if sid:
        headers['mcp-session-id'] = sid
    async with http.session().post(settings().mes_mcp_url, json=body, headers=headers, timeout=TIMEOUT) as r:
        return r.status, r.headers.get('mcp-session-id', sid), _message(await r.text(errors='replace'))


async def _session(token: str, fresh: bool) -> str:
    at, sid = _sessions.get(token, (0.0, ''))
    if sid and not fresh and time.time() - at < TTL_S:
        return sid
    init = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
            'params': {'protocolVersion': PROTOCOL, 'capabilities': {}, 'clientInfo': {'name': 'aihub', 'version': '1'}}}
    status, sid, message = await _post(token, init)
    if status == 401:
        raise MesError('MES rejected the token.')
    if status != 200 or not message or 'result' not in message:
        raise MesError(f'MES did not start a session ({status}).')
    await _post(token, {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, sid)
    _keep(_sessions, token, (time.time(), sid))
    return sid


_ids = itertools.count(2)  # 1 is the initialize call


async def _rpc(token: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    # The model's tool calls of one step run at the same time over one shared MCP session: each request needs its
    # own id, or the server hands one call the answer of another (seen: job A's details returned for job B).
    request_id = next(_ids)
    for fresh in (False, True):  # an expired session answers 400/404: start a new one, once
        sid = await _session(token, fresh)
        status, _, message = await _post(token, {'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params or {}}, sid)
        if status not in (400, 404):
            break
    if status == 401:
        raise MesError('MES rejected the token.')
    if status != 200 or not message:
        raise MesError(f'MES answered {status}.')
    if message.get('id') != request_id:  # never pass on an answer to some other request
        raise MesError('MES answered a different request.')
    if 'error' in message:
        raise MesError((message['error'] or {}).get('message', 'error'))
    return message['result']


def tool_name(spec: dict[str, Any]) -> str:
    name = str(spec.get('name', ''))
    return name if name.startswith('mes_') else f'mes_{name}'  # the server's own names may already carry the prefix


def is_read_tool(spec: dict[str, Any]) -> bool:
    """Read tools only: the server marks it readOnlyHint, or it is on the reviewed MES_READ_TOOLS list."""
    hints = spec.get('annotations') or {}
    allowed = {n.strip() for n in settings().mes_read_tools.split(',') if n.strip()}
    return hints.get('readOnlyHint') is True or tool_name(spec) in allowed


_SCHEMA_KEYS = ('type', 'description', 'enum', 'items', 'properties', 'required', 'minimum', 'maximum', 'format')


def _plain(node: Any) -> dict[str, Any]:
    """One property schema cut down to plain JSON-schema keys a function tool accepts; anything odd becomes a string."""
    if not isinstance(node, dict):
        return {'type': 'string'}
    out = {k: v for k, v in node.items() if k in _SCHEMA_KEYS}
    kind = out.get('type')
    if isinstance(kind, list):  # ["string", "null"] -> "string"
        out['type'] = next((t for t in kind if t != 'null'), 'string')
    elif not isinstance(kind, str):
        out['type'] = 'string'
    if isinstance(out.get('description'), str):
        out['description'] = out['description'][:300]
    if out['type'] == 'array':
        out['items'] = _plain(out.get('items'))
    if out['type'] == 'object':
        props = out.get('properties') if isinstance(out.get('properties'), dict) else {}
        out['properties'] = {str(k): _plain(v) for k, v in props.items()}
        out['required'] = [r for r in out.get('required') or [] if r in out['properties']]
    return out


def parameters(spec: dict[str, Any]) -> dict[str, Any]:
    schema = spec.get('inputSchema') or {}
    return _plain({**schema, 'type': 'object'})


def _make(token: str, spec: dict[str, Any]) -> Tool:
    async def call(ctx: TurnContext, **args: Any) -> str:
        started = time.monotonic()
        args = {k: v for k, v in args.items() if v not in ('', None)}  # the model fills optional fields with "": the MES reads Shift="" as a filter ("next shift") and finds nothing
        try:
            result = await _rpc(token, 'tools/call', {'name': spec['name'], 'arguments': args})
        except Exception as e:  # noqa: BLE001  a failing MES call must come back as text for the model, never crash the turn
            logger.warning('mes tool=%s user=%s ms=%d ok=False error=%s', spec['name'], ctx.user_id, (time.monotonic() - started) * 1000, type(e).__name__)
            return json.dumps({'error': f'MES: {e}'})
        text = clean('\n'.join(c.get('text', '') for c in result.get('content') or [] if c.get('type') == 'text'))
        failed = bool(result.get('isError'))
        logger.info('mes tool=%s user=%s ms=%d ok=%s', spec['name'], ctx.user_id, (time.monotonic() - started) * 1000, not failed)
        return json.dumps({'error': text or 'The MES tool failed.'}) if failed else text

    description = str(spec.get('description', ''))[:DESCRIPTION_MAX]
    return Tool(tool_name(spec), description, parameters(spec), call)


async def _fetch(token: str) -> list[dict[str, Any]]:
    try:
        specs = (await _rpc(token, 'tools/list'))['tools']
    except Exception as e:
        _keep(_failed, token, time.time())
        logger.warning('mes tools/list failed: %s', type(e).__name__)
        raise
    kept = [s for s in specs if is_read_tool(s)]
    if dropped := sorted(tool_name(s) for s in specs if not is_read_tool(s)):  # a count, not 100+ names on every refresh
        logger.info('%d MES tools not offered (not marked read-only, not on MES_READ_TOOLS), e.g. %s', len(dropped), ', '.join(dropped[:5]))
    _failed.pop(token, None)
    _keep(_specs, token, (time.time(), kept))
    return kept


async def _refresh(token: str) -> None:
    try:
        with contextlib.suppress(Exception):  # logged in _fetch; the known list stays until the next try
            await _fetch(token)
    finally:
        _refreshing.discard(token)


async def tools(ctx: TurnContext) -> list[Tool]:
    """The user's MES read tools; none without a token, or when MES failed in the last minute."""
    user_token = token(ctx)
    if not user_token or time.time() - _failed.get(user_token, 0.0) < FAILED_S:
        return []
    at, specs = _specs.get(user_token, (0.0, []))
    if not at:  # first use: wait once (connect gives up after 3 s)
        try:
            specs = await _fetch(user_token)
        except Exception:  # noqa: BLE001  MES down or token rejected: the chat works without MES tools
            return []
    elif time.time() - at > TTL_S and user_token not in _refreshing:  # known but old: use it, refresh behind the turn
        _refreshing.add(user_token)
        task = asyncio.get_running_loop().create_task(_refresh(user_token))
        _background.add(task)  # a task nobody holds may be collected before it finishes
        task.add_done_callback(_background.discard)
    unique = {tool_name(s): s for s in reversed(specs)}  # the server lists some tools twice (list_x and mes_list_x): keep the first
    return [_make(user_token, s) for s in specs if unique.get(tool_name(s)) is s]
