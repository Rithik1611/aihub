"""App notes: facts about how cableCORE works, verified in a chat and shared with every later chat.

Only facts proven by DesignCenter's own code (the files the agent read in that turn, checked by the server) or
written by an administrator are shared; anything else goes to the user's personal memory (tools.cc_note).
A code fact keeps its source files with a fingerprint of each; when a file changes or disappears (a new
release, re-indexed), the note is dropped and logged, and the agent learns the new behaviour from the code.

The file is plain Markdown, newest first; people can read and edit it. Only the newest that fit NOTES_CHARS
go into each request, without their sources (the agent never quotes file paths to users).
"""

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import Awaitable, Callable

from ai.settings import settings

NOTES_FILE = settings().cc_notes_file
NOTES_CHARS = 6000
MAX_NOTES = 200
CHECK_EVERY_S = 600  # how often sources are re-checked against the indexed code
_HEADER = '# cableCORE app notes\n\nFacts verified in chats (newest first). Edit or delete freely.\n\n'
_SOURCES = re.compile(r'\s*<!-- sources: (.*?) -->\s*$')

log = logging.getLogger(__name__)
_checked_at = 0.0

# path -> file text, '' if the file is gone, None if it cannot be checked now (code search down)
Fetch = Callable[[str], Awaitable[str | None]]


def fingerprint(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def _entries() -> list[str]:
    if not NOTES_FILE.exists():
        return []
    body = NOTES_FILE.read_text(encoding='utf-8').split('\n\n', 2)[-1] if NOTES_FILE.stat().st_size else ''
    return [e.strip().removeprefix('- ') for e in body.split('\n- ') if e.strip() and not e.startswith('#')]


def _write(entries: list[str]) -> None:
    NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOTES_FILE.write_text(_HEADER + ''.join(f'- {e}\n' for e in entries[:MAX_NOTES]), encoding='utf-8')


def _sources(entry: str) -> dict[str, str]:
    """{path: fingerprint} of a code fact; {} for an administrator's note."""
    m = _SOURCES.search(entry)
    return dict(s.rsplit('@', 1) for s in m.group(1).split(', ')) if m else {}


def add(fact: str, evidence: str, sources: dict[str, str] | None = None) -> str:
    fact, evidence = ' '.join(fact.split())[:400], ' '.join(evidence.split())[:300]
    if not fact:
        return 'Nothing to note.'
    entries = _entries()
    if any(fact.lower() in e.lower() for e in entries):
        return 'Already noted.'
    entry = f"{fact} (evidence: {evidence}; {time.strftime('%Y-%m-%d')})"
    if sources:
        entry += ' <!-- sources: ' + ', '.join(f'{p}@{h}' for p, h in sources.items()) + ' -->'
    _write([entry, *entries])
    return 'Noted for every later chat.'


async def prune(fetch: Fetch) -> list[str]:
    """Drops code facts whose source files changed; -> the dropped facts."""
    entries = _entries()
    paths = sorted({p for e in entries for p in _sources(e)})
    texts = dict(zip(paths, await asyncio.gather(*(fetch(p) for p in paths)), strict=True))
    stale = [e for e in entries
             if any(texts[p] is not None and (not texts[p] or fingerprint(texts[p] or '') != h) for p, h in _sources(e).items())]
    if stale:
        _write([e for e in entries if e not in stale])
        for e in stale:
            log.info('app note dropped, its source code changed: %s', _SOURCES.sub('', e))
    return stale


async def for_prompt(fetch: Fetch) -> str:
    global _checked_at
    if time.monotonic() - _checked_at > CHECK_EVERY_S:
        _checked_at = time.monotonic()
        await prune(fetch)
    out, size = [], 0
    for entry in _entries():
        line = '- ' + _SOURCES.sub('', entry)
        if size + len(line) > NOTES_CHARS:
            break
        out.append(line)
        size += len(line)
    return '\n'.join(out)

