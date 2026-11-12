"""App notes: facts about cableCORE the assistant verified in a chat, shared with every later chat.

The assistant writes them (cc_note) with the evidence; people can read and edit the file. Newest first;
only the newest that fit NOTES_CHARS go into each request.
"""

import time
from ai.settings import settings

NOTES_FILE = settings().cc_notes_file
NOTES_CHARS = 6000
_HEADER = '# cableCORE app notes\n\nFacts verified in chats (newest first). Edit or delete freely.\n\n'


def _entries() -> list[str]:
    if not NOTES_FILE.exists():
        return []
    body = NOTES_FILE.read_text(encoding='utf-8').split('\n\n', 2)[-1] if NOTES_FILE.stat().st_size else ''
    return [e.strip() for e in body.split('\n- ') if e.strip() and not e.startswith('#')]


def add(fact: str, evidence: str) -> str:
    fact, evidence = ' '.join(fact.split())[:400], ' '.join(evidence.split())[:300]
    if not fact:
        return 'Nothing to note.'
    entries = [e.lstrip('- ') for e in _entries()]
    if any(fact.lower() in e.lower() for e in entries):
        return 'Already noted.'
    entry = f"{fact} (evidence: {evidence}; {time.strftime('%Y-%m-%d')})"
    NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOTES_FILE.write_text(_HEADER + '\n'.join(f'- {e}' for e in [entry, *entries][:200]) + '\n', encoding='utf-8')
    return 'Noted for later chats.'


def for_prompt() -> str:
    out, size = [], 0
    for entry in _entries():
        line = entry if entry.startswith('- ') else f'- {entry}'
        if size + len(line) > NOTES_CHARS:
            break
        out.append(line)
        size += len(line)
    return '\n'.join(out)
