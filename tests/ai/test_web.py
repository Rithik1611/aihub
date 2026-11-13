"""fetch_url never reaches this machine or an internal network (SSRF), directly or through a redirect."""

import asyncio
import json

from ai import http, web


def fetch(url: str) -> dict[str, str]:
    async def go() -> str:
        try:
            return await web.fetch_url(None, url)  # type: ignore[arg-type]  # ctx is unused
        finally:
            await http.close()

    return json.loads(asyncio.run(go()))


def test_internal_addresses_are_refused():
    for url in ('http://localhost:5000/account/profile', 'http://127.0.0.1:8070/files/list', 'http://169.254.169.254/latest/meta-data/',
                'http://10.0.0.5/', 'http://[::1]:8080/', 'file:///etc/passwd'):
        error = fetch(url)['error']
        assert 'internal address' in error or 'Only http(s)' in error, (url, error)


def test_pdf_text_is_extracted():
    from io import BytesIO

    from pypdf import PdfWriter

    out = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(out)
    assert web._pdf_text(out.getvalue()).startswith('[page 1]')  # pyright: ignore[reportPrivateUsage]
