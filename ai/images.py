"""search_images (SerpAPI Google Images): pictures the model can show in the chat, e.g. a cable construction.

Only Google's own thumbnails are returned (https, encrypted-tbnN.gstatic.com), never the original image on some
third-party site: the user's browser then loads from Google, not from a page the model was steered to, and the chat's
sanitizer (cableCORE_FE utils/markdown.ts) allows exactly those hosts. Each picture comes with its source page, to
credit. SafeSearch is on. The key stays here; none without SERPAPI_KEY.
"""

import json
import re
import time
from collections import deque
from urllib.parse import urlparse

import aiohttp

from ai import http
from ai.context import TurnContext
from ai.settings import settings
from ai.tools import Tool, tool

MAX_IMAGES = 4
CACHE_TTL_S = 3600  # the same picture question within the hour costs no second SerpAPI search
CACHE_MAX = 500  # entries kept; the oldest goes first, so memory cannot grow without bound
PER_USER_PER_HOUR = 10  # ponytail: per process and in memory; a shared store (Valkey) when aihub runs several replicas
THUMB_HOST = re.compile(r'^encrypted-tbn\d\.gstatic\.com$')
_cache: dict[str, tuple[float, str]] = {}
_usage: dict[str, deque[float]] = {}  # user id -> times of their paid searches in the last hour


def _remember(key: str, value: tuple[float, str]) -> None:
    if key not in _cache and len(_cache) >= CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = value


def _may_search(user_id: str) -> bool:
    """Each paid search counts against the user: one person cannot spend the whole SerpAPI quota."""
    now = time.time()
    if user_id not in _usage and len(_usage) >= 1000:
        _usage.pop(next(iter(_usage)))
    searches = _usage.setdefault(user_id, deque())
    while searches and now - searches[0] > 3600:
        searches.popleft()
    if len(searches) >= PER_USER_PER_HOUR:
        return False
    searches.append(now)
    return True


def _https(url: str) -> bool:
    parsed = urlparse(url or '')
    return parsed.scheme == 'https' and bool(parsed.hostname)


async def search_images(ctx: TurnContext, query: str, count: int = MAX_IMAGES) -> str:
    """
    Find pictures on Google Images and show them to the user. Decide yourself when a picture helps, without waiting
    to be asked: whenever the answer is about what a cable or part looks like or is built from (its construction,
    layers, cross-section, how two types differ, a connector, a standard's diagram), or the user asks to see it.
    A real picture beats a description; never draw ASCII or text diagrams instead. Skip it for answers that are only
    numbers, lists of designs or data. It costs a paid search, so call it at most once per reply, for the main subject.
    Show the pictures in your reply as markdown, `![title](thumbnail)`, each followed by a link to its page, put
    where they belong in the answer, and say they come from the web (their exact construction varies by maker).
    Use only the thumbnail and page URLs returned here; never invent an image URL.

    :param query: what to find, in a few words (include "cable" or the product type)
    :param count: how many pictures, up to 4
    :return: JSON list of {title, thumbnail, page, source}
    """
    count = max(1, min(count or MAX_IMAGES, MAX_IMAGES))
    key = f'{query.strip().lower()}|{count}'
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_S:
        return hit[1]
    if not _may_search(ctx.user_id):
        return json.dumps({'error': f'Picture search limit reached ({PER_USER_PER_HOUR} per hour). Answer in words, or try again later.'})
    params = {'engine': 'google_images', 'q': query, 'safe': 'active', 'api_key': settings().serpapi_key.get_secret_value()}
    try:
        async with http.session().get('https://serpapi.com/search.json', params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return json.dumps({'error': f'Image search failed ({r.status}).'})  # never echo the URL: it carries the key
            data = await r.json()
    except Exception as e:
        return json.dumps({'error': f'Image search failed ({type(e).__name__}).'})
    rows = []
    for item in data.get('images_results') or []:
        thumb, page = item.get('thumbnail') or '', item.get('link') or ''
        if _https(thumb) and THUMB_HOST.match(urlparse(thumb).hostname or '') and _https(page):
            rows.append({'title': str(item.get('title') or '')[:120], 'thumbnail': thumb, 'page': page, 'source': str(item.get('source') or '')[:60]})
        if len(rows) == count:
            break
    text = json.dumps(rows, ensure_ascii=False) if rows else json.dumps({'error': 'No pictures found. Try other key words.'})
    if rows:
        _remember(key, (time.time(), text))
    return text


def tools() -> list[Tool]:
    return [tool(search_images)] if settings().serpapi_key.get_secret_value() else []
