"""MES MCP tools against a fake streamable-HTTP MCP server: per-user tokens, SSE answers, expired-session retry."""

import json
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import SecretStr

from ai import mes
from ai.context import TurnContext

TOOLS = [{'name': 'get_job_detail', 'description': 'One job.', 'inputSchema': {
    'type': 'object', 'properties': {'JobCode': {'type': 'string'}}, 'required': ['JobCode']}}]


def fake_mes(state):
    async def handle(request):
        if request.headers.get('Authorization') != 'Bearer good':
            return web.json_response({'error': 'no'}, status=401)
        body = await request.json()
        method, headers = body.get('method'), {}
        if method == 'initialize':
            state['sessions'] += 1
            headers['mcp-session-id'] = f"s{state['sessions']}"
            result = {'protocolVersion': mes.PROTOCOL, 'capabilities': {}}
        elif method == 'notifications/initialized':
            return web.Response(status=202)
        elif request.headers.get('mcp-session-id') != f"s{state['sessions']}":
            return web.json_response({'error': 'session'}, status=404)
        elif method == 'tools/list':
            result = {'tools': TOOLS}
        else:
            result = {'content': [{'type': 'text', 'text': f"job {body['params']['arguments']['JobCode']} running"}]}
        text = f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result})}\n\n"
        return web.Response(text=text, content_type='text/event-stream', headers=headers)

    app = web.Application()
    app.router.add_post('/mcp', handle)
    return app


def user(email):
    return TurnContext(user_id='1', user_email=email, cc_token='', chat_id='c', wait_answer=None)  # type: ignore[arg-type]


async def test_mes_tools_are_per_user(monkeypatch):
    state = {'sessions': 0}
    async with TestServer(fake_mes(state)) as server:
        conf = SimpleNamespace(mes_mcp_url=str(server.make_url('/mcp')), mes_user_tokens=SecretStr(json.dumps({'DCAdmin': 'good'})))
        monkeypatch.setattr(mes, 'settings', lambda: conf)
        mes._sessions.clear()
        mes._specs.clear()

        assert await mes.tools(user('someone-else')) == []  # no token: no MES tools, no error
        assert await mes.instructions(user('someone-else')) == ''

        [tool] = await mes.tools(user('dcadmin'))  # username match ignores case
        assert tool.name == 'mes_get_job_detail' and tool.parameters['required'] == ['JobCode']
        assert await tool.fn(user('dcadmin'), JobCode='J-1') == 'job J-1 running'

        state['sessions'] += 1  # the server forgets our session: one transparent re-initialise
        assert await tool.fn(user('dcadmin'), JobCode='J-2') == 'job J-2 running'

        bad = SimpleNamespace(**{**vars(conf), 'mes_user_tokens': SecretStr(json.dumps({'DCAdmin': 'revoked'}))})
        monkeypatch.setattr(mes, 'settings', lambda: bad)
        mes._specs.clear()
        assert await mes.tools(user('DCAdmin')) == []  # rejected token: chat works without MES


async def test_parallel_calls_get_their_own_ids_and_a_wrong_reply_is_never_passed_on(monkeypatch):
    import asyncio

    seen_ids, mix_up = [], {'on': False}

    async def handle(request):
        body = await request.json()
        method, headers = body.get('method'), {}
        if method == 'initialize':
            headers['mcp-session-id'] = 's1'
            result = {'protocolVersion': mes.PROTOCOL, 'capabilities': {}}
        elif method == 'notifications/initialized':
            return web.Response(status=202)
        elif method == 'tools/list':
            result = {'tools': TOOLS}
        else:
            seen_ids.append(body['id'])
            result = {'content': [{'type': 'text', 'text': f"job {body['params']['arguments']['JobCode']}"}]}
        reply_id = body['id'] + 1000 if mix_up['on'] and method == 'tools/call' else body['id']  # a server answering the wrong request
        text = f"data: {json.dumps({'jsonrpc': '2.0', 'id': reply_id, 'result': result})}\n\n"
        return web.Response(text=text, content_type='text/event-stream', headers=headers)

    app = web.Application()
    app.router.add_post('/mcp', handle)
    async with TestServer(app) as server:
        conf = SimpleNamespace(mes_mcp_url=str(server.make_url('/mcp')), mes_user_tokens=SecretStr(json.dumps({'DCAdmin': 'good'})))
        monkeypatch.setattr(mes, 'settings', lambda: conf)
        mes._sessions.clear()
        mes._specs.clear()
        [tool] = await mes.tools(user('DCAdmin'))

        outs = await asyncio.gather(*(tool.fn(user('DCAdmin'), JobCode=f'J{i}') for i in range(6)))
        assert outs == [f'job J{i}' for i in range(6)]  # each call got its own answer
        assert len(set(seen_ids)) == 6  # ...because each request carried its own id

        mix_up['on'] = True
        wrong = await tool.fn(user('DCAdmin'), JobCode='J9')
        assert 'different request' in wrong and 'job J9' not in wrong  # an error, never someone else's data
