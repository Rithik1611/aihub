"""The assistant's database login must not read credentials or secrets, whatever our code does (ai_reader.sql).
Fails until the database owner has run ai/apps/cablecore/ai_reader.sql on this database; also catches new
secret-like columns that the script does not cover yet."""

import os

import psycopg
import pytest

SECRET_COLUMN = r'(passw|secret|refreshtoken|resetpassword|codehash|key_hash|clientsecret)'


def _connect() -> psycopg.Connection:
    url = os.environ.get('CC_DB_URL', '')
    if not url:
        pytest.skip('no CC_DB_URL')
    try:
        return psycopg.connect(url, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip('database not reachable')


def test_ai_reader_cannot_read_secrets():
    with _connect() as c:
        readable = c.execute(
            """select table_name || '.' || column_name from information_schema.columns
                where table_schema = 'public' and column_name ~* %s
                  and has_column_privilege(format('public.%%I', table_name), column_name, 'SELECT')""",
            (SECRET_COLUMN,)).fetchall()
    assert readable == [], f'ai_reader can read secrets; run ai_reader.sql as the database owner: {readable}'
