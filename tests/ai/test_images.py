"""search_images against a fake SerpAPI: only Google thumbnails over https, SafeSearch on, cached, key never echoed."""

import json
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import SecretStr

from ai import images
from ai.context import TurnContext

GOOD = 'https://encrypted-tbn0.gstatic.com/images?q=tbn:abc'
RESULTS = {'images_results': [
    {'title': 'LiHCH', 'thumbnail': GOOD, 'link': 'https://example.com/lihch', 'source': 'example.com'},
    {'title': 'tracker', 'thumbnail': 'https://evil.example/pixel.png', 'link': 'https://example.com/x'},  # not Google's host
    {'title': 'plain http', 'thumbnail': 'http://encrypted-tbn0.gstatic.com/x', 'link': 'https://example.com/y'},
    {'title': 'js page', 'thumbnail': GOOD, 'link': 'javascript:alert(1)'},
    {'title': 'LiHH', 'thumbnail': 'https://encrypted-tbn2.gstatic.com/images?q=tbn:def', 'link': 'https://example.com/lihh'},
]}


def ctx():
    return TurnContext(user_id='1', user_email='u', cc_token='', chat_id='c', wait_answer=None)  # type: ignore[arg-type]


async def test_only_google_thumbnails_come_back(monkeypatch):
    seen = []

    async def handle(request):
        seen.append(dict(request.query))
        return web.json_response(RESULTS)

    app = web.Application()
    app.router.add_get('/search.json', handle)
    async with TestServer(app) as server:
        real = images.http.session

        class Session:  # point the fixed SerpAPI URL at the fake
            def get(self, url, **kw):
                return real().get(str(server.make_url('/search.json')), **kw)

        monkeypatch.setattr(images.http, 'session', lambda: Session())
        monkeypatch.setattr(images, 'settings', lambda: SimpleNamespace(serpapi_key=SecretStr('k3y')))
        images._cache.clear()

        rows = json.loads(await images.search_images(ctx(), 'LiHCH cable construction'))
        assert [r['title'] for r in rows] == ['LiHCH', 'LiHH']  # tracker, http and javascript: results dropped
        assert seen[0]['safe'] == 'active' and seen[0]['engine'] == 'google_images'

        await images.search_images(ctx(), 'lihch cable construction')  # same question: cached, no second paid search
        assert len(seen) == 1
        assert 'k3y' not in json.dumps(rows)


def test_no_key_no_tool(monkeypatch):
    monkeypatch.setattr(images, 'settings', lambda: SimpleNamespace(serpapi_key=SecretStr('')))
    assert images.tools() == []


async def test_show_drawing_points_at_the_backend_route(monkeypatch):
    import importlib

    cc_tools = importlib.import_module('ai.apps.cablecore.tools')  # `from ai.apps.cablecore import tools` is the pack's function

    monkeypatch.setattr(cc_tools, 'settings', lambda: SimpleNamespace(cc_public_url='http://localhost:5000/'))
    signed_in = TurnContext(user_id='1', user_email='u', cc_token='jwt', chat_id='c', wait_answer=None)  # type: ignore[arg-type]
    design = '3f1c2a4e-9b7d-4c1a-8e2f-0a1b2c3d4e5f'
    assert await cc_tools.cc_show_drawing(signed_in, design) == f'![Design drawing](http://localhost:5000/erp/{design}/drawingurl)'
    assert 'UUID' in await cc_tools.cc_show_drawing(signed_in, 'LV-7034')  # a design code, not its id: no URL is built from it


async def test_one_user_cannot_spend_the_quota_and_the_cache_stays_bounded(monkeypatch):
    calls = []

    async def handle(request):
        calls.append(request.query['q'])
        return web.json_response(RESULTS)

    app = web.Application()
    app.router.add_get('/search.json', handle)
    async with TestServer(app) as server:
        real = images.http.session

        class Session:
            def get(self, url, **kw):
                return real().get(str(server.make_url('/search.json')), **kw)

        monkeypatch.setattr(images.http, 'session', lambda: Session())
        monkeypatch.setattr(images, 'settings', lambda: SimpleNamespace(serpapi_key=SecretStr('k')))
        monkeypatch.setattr(images, 'CACHE_MAX', 3)
        images._cache.clear()
        images._usage.clear()

        busy = TurnContext(user_id='busy', user_email='u', cc_token='', chat_id='c', wait_answer=None)  # type: ignore[arg-type]
        for i in range(images.PER_USER_PER_HOUR):
            assert 'error' not in await images.search_images(busy, f'cable {i}')
        assert 'limit reached' in await images.search_images(busy, 'one more')  # the 11th paid search is refused
        assert len(calls) == images.PER_USER_PER_HOUR  # ...and never reached SerpAPI

        other = TurnContext(user_id='other', user_email='u', cc_token='', chat_id='c', wait_answer=None)  # type: ignore[arg-type]
        assert 'error' not in await images.search_images(other, 'another question')  # a different user is unaffected
        assert len(images._cache) <= 3  # the oldest answers were dropped
