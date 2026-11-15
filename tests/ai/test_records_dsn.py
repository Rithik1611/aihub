"""The read-only database address: rewritten to localhost on the host, left alone inside a container."""

import importlib
from types import SimpleNamespace

from pydantic import SecretStr

records = importlib.import_module('ai.apps.cablecore.records')  # `from ai.apps.cablecore import records` is fine too; explicit for clarity

DSN = 'postgresql://ai_reader:pw@host.docker.internal:5433/taisin'


def configure(monkeypatch, in_container: bool) -> None:
    monkeypatch.setattr(records, 'settings', lambda: SimpleNamespace(cc_db_url=SecretStr(DSN)))
    monkeypatch.setattr(records, '_in_container', lambda: in_container)


def test_on_the_host_the_docker_name_becomes_localhost(monkeypatch):
    configure(monkeypatch, in_container=False)
    assert records._dsn() == 'postgresql://ai_reader:pw@localhost:5433/taisin'


def test_in_a_container_the_docker_name_is_kept(monkeypatch):
    configure(monkeypatch, in_container=True)  # localhost there is the container itself, not the Postgres on the host
    assert records._dsn() == DSN


def test_other_hosts_are_never_touched(monkeypatch):
    monkeypatch.setattr(records, 'settings', lambda: SimpleNamespace(cc_db_url=SecretStr('postgresql://u:p@db.internal:5432/x')))
    for flag in (True, False):
        monkeypatch.setattr(records, '_in_container', lambda flag=flag: flag)
        assert records._dsn() == 'postgresql://u:p@db.internal:5432/x'
