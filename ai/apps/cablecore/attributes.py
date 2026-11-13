"""Searchable attribute keys, from the design search index itself (no hand-written mapping).

The index lists every key it can filter on (/os/autocomplete/attribute-names: design-level keys, and process keys
as process.key, lowercase; search accepts any case). The database adds what the index does not say: each key's
label (attributeDesc), type, how many designs have a value, and the most common values.
Used by cc_attributes (find the key for a property before filtering) and by cc_search (flag a filter whose key
is not in the index).
"""

import asyncio
import time
from collections import Counter
from typing import Any

from ai.apps.cablecore import api, records

TTL_S = 1800
VALUES_SHOWN = 8

_keys: tuple[float, frozenset[str]] = (0.0, frozenset())
_meta: tuple[float, dict[str, dict[str, Any]]] = (0.0, {})


async def index_keys(token: str) -> frozenset[str]:
    """Every key the design search index can filter on, lowercase (process keys as process.key)."""
    global _keys
    if time.monotonic() - _keys[0] > TTL_S or not _keys[1]:
        found: set[str] = set()
        for category in ('plant', 'process'):
            # ponytail: the route caps size at 5000 (2.3k process keys today); page it if a category ever reaches 5000
            status, data = await api.call('GET', '/os/autocomplete/attribute-names', token, query={'category': category, 'size': 5000})
            if api.failed(status, data):
                raise api.CableCoreError(f'Could not read the search index keys: {api.failed(status, data)}')
            found |= {str(n).lower() for n in (data or {}).get('names') or []}
        _keys = (time.monotonic(), frozenset(found))
    return _keys[1]


def _load_meta() -> dict[str, dict[str, Any]]:
    """key (lowercase) -> name as stored, label, type; from the attribute definitions."""
    meta: dict[str, dict[str, Any]] = {}
    for r in records._rows('select distinct name, "attributeDesc" as label, "attributeType" as type from "PlantAttribute"'):
        meta.setdefault(r['name'].lower(), {'key': r['name'], 'label': r['label'], 'type': r['type']})
    for r in records._rows('select distinct pr.name as process, a.name, a."attributeDesc" as label, a."attributeType" as type '
                           'from "ProcessAttribute" a join "Process" pr on pr.id = a."processId"'):
        meta.setdefault(f"{r['process']}.{r['name']}".lower(), {'key': f"{r['process']}.{r['name']}", 'label': r['label'], 'type': r['type']})
    return meta


async def meta() -> dict[str, dict[str, Any]]:
    global _meta
    if time.monotonic() - _meta[0] > TTL_S or not _meta[1]:
        _meta = (time.monotonic(), await asyncio.to_thread(_load_meta))
    return _meta[1]


def _values(key: str) -> tuple[int, list[tuple[str, int]]]:
    """How many designs have a value for this key, and the most common values, from the database."""
    if '.' in key:
        process, name = key.split('.', 1)
        rows = records._rows('select v.* from "ProcessAttributeValue" v join "ProcessAttribute" a on a.id = v."processAttributeId" '
                             'join "Process" pr on pr.id = a."processId" where pr.name = %s and a.name = %s', (process, name))
    else:
        rows = records._rows('select v.* from "PlantAttributeValue" v join "PlantAttribute" a on a.id = v."plantAttributeId" '
                             'where a.name = %s', (key,))
    counts = Counter(str(v) for v in (records._value(r) for r in rows) if v not in (None, ''))
    return sum(counts.values()), counts.most_common(VALUES_SHOWN)


async def values(key: str) -> tuple[int, list[tuple[str, int]]]:
    return await asyncio.to_thread(_values, key)


def _usage(keys: list[str]) -> dict[str, int]:
    """Stored value rows per key (lowercase), in one query per kind: to rank many matches cheaply."""
    plain = [k for k in keys if '.' not in k]
    process = [k for k in keys if '.' in k]
    usage: dict[str, int] = {}
    if plain:
        for r in records._rows('select lower(a.name) as k, count(*) as n from "PlantAttributeValue" v join "PlantAttribute" a '
                               'on a.id = v."plantAttributeId" where lower(a.name) = any(%s) group by 1', (plain,)):
            usage[r['k']] = r['n']
    if process:
        for r in records._rows('select lower(pr.name || \'.\' || a.name) as k, count(*) as n from "ProcessAttributeValue" v '
                               'join "ProcessAttribute" a on a.id = v."processAttributeId" join "Process" pr on pr.id = a."processId" '
                               'where lower(pr.name || \'.\' || a.name) = any(%s) group by 1', (process,)):
            usage[r['k']] = r['n']
    return usage


async def find(token: str, words: list[str], process: str = '', limit: int = 15) -> tuple[int, list[dict[str, Any]]]:
    """Index keys whose name or label contains any of the words, most used first, with their common values.
    Returns (number of matching keys, the top `limit` of them)."""
    keys, info = await index_keys(token), await meta()
    wanted = [w.lower().strip() for w in words if w.strip()]
    prefix = f'{process.lower()}.' if process else ''
    hits = []
    for key in keys:
        if prefix and not key.startswith(prefix):
            continue
        label = str((info.get(key) or {}).get('label') or '').lower()
        if any(w in key or w in label for w in wanted):
            hits.append(key)
    usage = await asyncio.to_thread(_usage, hits)
    top = sorted(hits, key=lambda k: -usage.get(k, 0))[:limit]
    found = await asyncio.gather(*(values((info.get(k) or {}).get('key', k)) for k in top))
    rows = []
    for key, (count, common) in zip(top, found):
        i = info.get(key) or {}
        rows.append({'key': i.get('key', key), 'label': i.get('label'), 'type': i.get('type'), 'designs_with_value': count,
                     'common_values': [f'{v} ({n})' for v, n in common]})
    return len(hits), rows
