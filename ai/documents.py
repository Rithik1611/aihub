"""Text of an uploaded document, so the whole document can sit in the conversation instead of being re-read from
the sandbox every turn. PDF per page (scanned pages through OCR), spreadsheets per sheet (tab-separated), Word
paragraphs, plain text. Images are not turned into text: the model sees them (IMAGE_TYPES)."""

import asyncio
import csv
import html
import io
import re
import zipfile

from openpyxl import load_workbook
from pypdf import PdfReader

PAGES_MAX = 300
SHEET_ROWS_MAX = 2000
SCANNED_CHARS = 20  # a PDF page with less text than this is a scan (or a drawing)
IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif'}
TEXT_TYPES = ('.txt', '.md', '.csv', '.tsv', '.json', '.xml', '.html', '.htm', '.log', '.yaml', '.yml')
_PAGE = re.compile(r'\[page (\d+)\]\n')


def extract(name: str, content: bytes) -> tuple[str, int]:
    """(text, pages) of a document; ('', 0) for a format with no text to take (images, archives).
    Raises ValueError for a document that cannot be read (corrupt, encrypted)."""
    lower = name.lower()
    try:
        if lower.endswith('.pdf'):
            reader = PdfReader(io.BytesIO(content))
            pages = reader.pages[:PAGES_MAX]
            return '\n\n'.join(f'[page {i + 1}]\n{(p.extract_text() or "").strip()}' for i, p in enumerate(pages)), len(reader.pages)
        if lower.endswith(('.xlsx', '.xlsm')):
            book = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            parts = []
            for sheet in book.worksheets:
                rows = []
                for row in sheet.iter_rows(values_only=True, max_row=SHEET_ROWS_MAX):
                    if any(v not in (None, '') for v in row):
                        rows.append('\t'.join('' if v is None else str(v) for v in row).rstrip('\t'))
                parts.append(f'[sheet {sheet.title}]\n' + '\n'.join(rows))
            return '\n\n'.join(parts), len(book.worksheets)
        if lower.endswith('.docx'):
            xml = zipfile.ZipFile(io.BytesIO(content)).read('word/document.xml').decode('utf-8', 'replace')
            paragraphs = [re.sub(r'<[^>]+>', '', p) for p in re.findall(r'<w:p[ >].*?</w:p>', xml, re.S)]
            return '\n'.join(p for p in paragraphs if p.strip()), 1
        if lower.endswith(TEXT_TYPES):
            text = content.decode('utf-8', 'replace')
            if lower.endswith(('.csv', '.tsv')):  # normalise to tabs, the same shape as a sheet
                dialect = '\t' if lower.endswith('.tsv') else ','
                text = '\n'.join('\t'.join(r) for r in csv.reader(io.StringIO(text), delimiter=dialect))
            return text, 1
    except (KeyError, ValueError, zipfile.BadZipFile, OSError) as e:
        raise ValueError(f'{name} could not be read: {e}') from e
    except Exception as e:  # noqa: BLE001 - pypdf/openpyxl raise their own types for damaged files
        raise ValueError(f'{name} could not be read ({type(e).__name__}).') from e
    return '', 0


def image_mime(name: str) -> str | None:
    """The image type the model accepts for this file name, or None."""
    return next((m for ext, m in IMAGE_TYPES.items() if name.lower().endswith(ext)), None)


def _rows(table: str) -> str:
    """An HTML table (as OCR returns it) as one line per row, cells separated by ' | '."""
    rows = re.findall(r'<tr>(.*?)</tr>', table, re.S)
    return '\n'.join(' | '.join(html.unescape(re.sub(r'<[^>]+>', '', c)).strip()
                                for c in re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', r, re.S)) for r in rows)


def plain_tables(text: str) -> str:
    return re.sub(r'<table>.*?</table>', lambda m: _rows(m.group(0)), text, flags=re.S)


def _pages(text: str) -> dict[int, str]:
    parts = _PAGE.split(text)[1:]  # [number, text, number, text, ...]
    return {int(parts[i]): parts[i + 1].strip() for i in range(0, len(parts), 2)}


async def read(name: str, content: bytes) -> tuple[str, int, str]:
    """(text, pages, note) of a document. Scanned PDF pages are read by OCR when it is configured; note says what
    could not be read. Raises ValueError for a document that cannot be opened."""
    from ai import ocr  # here, not at import time: ocr needs the HTTP session

    text, pages = await asyncio.to_thread(extract, name, content)
    if not name.lower().endswith('.pdf') or not pages:
        return text, pages, ''
    by_page = _pages(text)
    scanned = [n for n, t in by_page.items() if len(t) < SCANNED_CHARS]
    if not scanned:
        return text, pages, ''
    if not ocr.available():
        return text, pages, f'{len(scanned)} page(s) are scanned images and no OCR is configured; their text is missing.'
    note = f'Only the first {ocr.PAGES_MAX} of {len(scanned)} scanned pages were read.' if len(scanned) > ocr.PAGES_MAX else ''
    try:
        found = await ocr.read_pages(content, scanned)
    except (ocr.OcrError, OSError, TimeoutError) as e:
        return text, pages, f'{len(scanned)} scanned page(s) could not be read: {e}'
    for n, t in found.items():
        by_page[n] = f'(read by OCR; may contain reading errors)\n{plain_tables(t)}'
    return '\n\n'.join(f'[page {n}]\n{by_page[n]}' for n in sorted(by_page)), pages, note


if __name__ == '__main__':
    assert extract('a.csv', b'x,y\n1,2\n') == ('x\ty\n1\t2', 1)
    assert extract('a.png', b'\x89PNG') == ('', 0)
    try:
        extract('a.pdf', b'not a pdf')
        raise AssertionError
    except ValueError:
        pass
    assert plain_tables('x<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2 &amp; 3</td></tr></table>y') == 'xA | B\n1 | 2 & 3y'
    assert _pages('[page 1]\nfoo\n\n[page 2]\n') == {1: 'foo', 2: ''}
    assert image_mime('Shot.PNG') == 'image/png' and image_mime('a.tiff') is None
    print('documents self-check OK')
