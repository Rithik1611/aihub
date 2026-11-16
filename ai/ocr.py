"""Text of scanned PDF pages, from Azure AI Document Intelligence (OCR_API_URL, OCR_API_KEY).

The layout model reads text and tables; its markdown output keeps tables as tables, which matters for specs and
RFQs. Analysis is asynchronous: the request answers 202 with an Operation-Location that is polled until done.
"""

import asyncio
import base64
import time

import aiohttp

from ai import http
from ai.settings import settings

API_VERSION = '2024-11-30'
POLL_S = 1.0
TIMEOUT_S = 120
PAGES_MAX = 50


class OcrError(Exception):
    pass


def available() -> bool:
    s = settings()
    return bool(s.ocr_api_url and s.ocr_api_key.get_secret_value())


async def read_pages(content: bytes, pages: list[int]) -> dict[int, str]:
    """Page number (1-based) -> markdown text, for the given pages of a PDF. Raises OcrError."""
    s = settings()
    headers = {'Ocp-Apim-Subscription-Key': s.ocr_api_key.get_secret_value()}
    url = (f"{s.ocr_api_url.rstrip('/')}/documentintelligence/documentModels/prebuilt-layout:analyze"
           f"?api-version={API_VERSION}&outputContentFormat=markdown&pages={','.join(map(str, pages[:PAGES_MAX]))}")
    body = {'base64Source': base64.b64encode(content).decode()}
    timeout = aiohttp.ClientTimeout(total=60)
    async with http.session().post(url, json=body, headers=headers, timeout=timeout) as r:
        if r.status != 202:
            raise OcrError(f'OCR refused the document ({r.status}): {(await r.text())[:200]}')
        operation = r.headers['Operation-Location']
    deadline = time.time() + TIMEOUT_S
    while time.time() < deadline:
        await asyncio.sleep(POLL_S)
        async with http.session().get(operation, headers=headers, timeout=timeout) as r:
            result = await r.json()
        state = result.get('status')
        if state == 'succeeded':
            return _by_page(result.get('analyzeResult') or {})
        if state == 'failed':
            raise OcrError(f"OCR failed: {str(result.get('error'))[:200]}")
    raise OcrError(f'OCR did not finish within {TIMEOUT_S} s.')


def _by_page(analysis: dict) -> dict[int, str]:
    """Splits the whole-document markdown into pages by each page's spans (offsets into `content`)."""
    content: str = analysis.get('content') or ''
    out: dict[int, str] = {}
    for page in analysis.get('pages') or []:
        text = ''.join(content[s['offset']: s['offset'] + s['length']] for s in page.get('spans') or [])
        out[int(page['pageNumber'])] = text.replace('<!-- PageBreak -->', '').strip()
    return out


if __name__ == '__main__':
    sample = {'content': 'A\n<!-- PageBreak -->\nB', 'pages': [{'pageNumber': 1, 'spans': [{'offset': 0, 'length': 1}]},
                                                               {'pageNumber': 2, 'spans': [{'offset': 2, 'length': 20}]}]}
    assert _by_page(sample) == {1: 'A', 2: 'B'}
    print('ocr self-check OK')
