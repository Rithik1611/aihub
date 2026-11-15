"""Text of an uploaded document, so the whole document can sit in the conversation instead of being re-read from
the sandbox every turn. PDF per page, spreadsheets per sheet (tab-separated), Word paragraphs, plain text."""

import csv
import io
import re
import zipfile

from openpyxl import load_workbook
from pypdf import PdfReader

PAGES_MAX = 300
SHEET_ROWS_MAX = 2000
TEXT_TYPES = ('.txt', '.md', '.csv', '.tsv', '.json', '.xml', '.html', '.htm', '.log', '.yaml', '.yml')


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


if __name__ == '__main__':
    assert extract('a.csv', b'x,y\n1,2\n') == ('x\ty\n1\t2', 1)
    assert extract('a.png', b'\x89PNG') == ('', 0)
    try:
        extract('a.pdf', b'not a pdf')
        raise AssertionError
    except ValueError:
        pass
    print('documents self-check OK')
