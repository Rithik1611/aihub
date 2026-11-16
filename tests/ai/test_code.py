import asyncio
import io
import json
import tarfile
import urllib.request

import pytest

from ai.apps.cablecore import code


def tar(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w') as t:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return out.getvalue()


def test_secret_gate_stops_real_credentials_not_test_fixtures():
    assert code.secret_hits(tar({'a.spec.ts': "const u = 'postgresql://postgres:secret@db:5432/x';"})) == []
    assert code.secret_hits(tar({'b.ts': "const u = 'postgresql://app:Xk9#2mPq@db:5432/x';"})) == ['b.ts:1']
    assert code.secret_hits(tar({'c.ts': 'ok\n-----BEGIN RSA PRIVATE KEY-----'})) == ['c.ts:2']


def _zoekt_up() -> bool:
    try:
        return urllib.request.urlopen(code.ZOEKT_URL, timeout=2).status == 200
    except OSError:
        return False


@pytest.mark.skipif(not _zoekt_up(), reason='needs the zoekt server with the index (start-zoekt.sh)')
def test_search_then_read_a_file():
    async def run():
        found = json.loads(await code.code_search(None, 'repo:cableCORE_BE sym:validateDropdown', 3))  # type: ignore[arg-type]
        f = found['files'][0]
        read = json.loads(await code.code_read(None, f['repo'], f['path'], f['snippets'][0]['line'], 0))  # type: ignore[arg-type]
        missing = json.loads(await code.code_read(None, 'cableCORE_BE', '../../etc/passwd'))  # type: ignore[arg-type]
        return found, read, missing

    found, read, missing = asyncio.run(run())
    assert found['matches'] >= 1 and found['files'][0]['snippets']
    assert read['lines_total'] > 0 and read['end_line'] - read['start_line'] < code.MAX_READ_LINES
    assert 'validateDropdown' in read['text'] or read['start_line'] > 1
    assert 'error' in missing
