"""search_web (DuckDuckGo) and fetch_url (a page's readable text)."""

import asyncio
import io
import ipaddress
import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import aiohttp
from ddgs import DDGS
from pypdf import PdfReader

from ai import http
from ai.context import TurnContext
from ai.tools import tool

RESULT_COUNT = 5
# Tried in this order until one returns results (ddgs backend names). google answers long queries too.
SEARCH_ENGINES = ('google', 'bing', 'brave', 'mojeek', 'yahoo', 'duckduckgo')
SHORT_QUERY_WORDS = 6
FETCH_MAX_CHARS = 50_000
FETCH_MAX_BYTES = 30_000_000
MAX_REDIRECTS = 5


async def search_web(ctx: TurnContext, query: str, count: int = RESULT_COUNT) -> str:
    """
    Search the public web for information. Best for current events, external references,
    or topics not covered in internal documents.

    :param query: The search query to look up
    :param count: Number of results to return (default: admin-configured value)
    :return: JSON with search results containing title, link, and snippet for each result
    """
    count = max(1, min(count or RESULT_COUNT, RESULT_COUNT))
    short = ' '.join(query.split()[:SHORT_QUERY_WORDS])
    rows = await asyncio.to_thread(_search, query, count)
    if not rows and short != query:  # long keyword lists return nothing on most engines
        rows = await asyncio.to_thread(_search, short, count)
    if not rows:
        return json.dumps({'error': 'No results found. Try a shorter query with the key terms only.'})
    return json.dumps([{'title': r.get('title'), 'link': r.get('href'), 'snippet': r.get('body')} for r in rows], ensure_ascii=False)


def _search(query: str, count: int) -> list[dict[str, str]]:
    """The first engine that answers. Free engines block or throttle some networks (DuckDuckGo
    challenges this one every time, Brave rate-limits), so they are tried in order, not at random."""
    for backend in SEARCH_ENGINES:
        try:
            rows = DDGS().text(query, max_results=count, backend=backend, safesearch='moderate')
        except Exception:
            continue
        if rows:
            return rows
    return []


class _Text(HTMLParser):
    SKIP = {'script', 'style', 'noscript', 'svg', 'head', 'nav', 'footer'}
    BLOCK = {'p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'section', 'article', 'table'}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        self.skipping += tag in self.SKIP
        if tag in self.BLOCK:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        self.skipping -= tag in self.SKIP and self.skipping > 0

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


async def fetch_url(ctx: TurnContext, url: str) -> str:
    """
    Fetch and extract the main text content from a web page URL.

    :param url: The URL to fetch content from
    :return: The extracted text content from the page
    """
    try:
        body, kind, final_url = await _get_public(url)
    except Exception as e:
        return json.dumps({'error': str(e)})
    if 'pdf' in kind or (not kind and final_url.lower().endswith('.pdf')) or body[:5] == b'%PDF-':
        try:
            text = await asyncio.to_thread(_pdf_text, body)
        except Exception as e:
            return json.dumps({'error': f'The PDF could not be read: {e}'})
    elif 'html' in kind or 'text' in kind or 'json' in kind or 'xml' in kind or not kind:
        text = body.decode(errors='replace')
        if 'html' in kind:
            parser = _Text()
            parser.feed(text)
            text = re.sub(r'\n\s*\n+', '\n\n', re.sub(r'[ \t]+', ' ', ''.join(parser.parts))).strip()
    else:
        return json.dumps({'error': f'{final_url} is a {kind} file, not a page or PDF; download it in the sandbox to read it.'})
    return text[:FETCH_MAX_CHARS] + ('\n\n[Content truncated...]' if len(text) > FETCH_MAX_CHARS else '')


def _pdf_text(content: bytes) -> str:
    reader = PdfReader(io.BytesIO(content))
    return '\n\n'.join(f'[page {i + 1}]\n{page.extract_text() or ""}' for i, page in enumerate(reader.pages[:60]))


async def _public_host(host: str) -> None:
    """Refuses hosts that resolve to this machine or an internal network (SSRF): the model may be steered
    by page or document content into fetching the cableCORE backend, the sandbox or cloud metadata."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global:
            raise ValueError(f'{host} is an internal address; only public web pages can be fetched.')


async def _get_public(url: str) -> tuple[bytes, str, str]:
    """-> (body, content type, final url); every redirect hop is checked too."""
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError('Only http(s) URLs can be fetched.')
        await _public_host(parsed.hostname)
        async with http.session().get(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; cableAI)'}, allow_redirects=False,
                                      timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status in (301, 302, 303, 307, 308) and r.headers.get('location'):
                url = urljoin(url, r.headers['location'])
                continue
            if r.status >= 400:
                raise ValueError(f'{r.status} from {url}')
            body = bytearray()
            async for chunk in r.content.iter_chunked(65536):
                body += chunk
                if len(body) > FETCH_MAX_BYTES:
                    raise ValueError(f'{url} is larger than {FETCH_MAX_BYTES // 1_000_000} MB')
            return bytes(body), r.headers.get('content-type', '').lower(), url
    raise ValueError(f'Too many redirects from {url}')


TOOLS = [tool(search_web, defer=True), tool(fetch_url, defer=True)]  # found by tool search when it is on
