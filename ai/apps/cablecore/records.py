"""Full records from the read-only database, for `cc_get` and the read-back after every change.

Read as the `ai_reader` role (SELECT only), so this never changes data.
"""

import asyncio
import json
import re
import time
from datetime import date, datetime
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row

from ai.settings import settings

_UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
MAX_CHARS = 9000


def _dsn() -> str:
    # The sandbox reaches Postgres as host.docker.internal; this process runs on the host.
    return settings().cc_db_url.get_secret_value().replace('host.docker.internal', 'localhost')


def _rows(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with psycopg.connect(_dsn(), connect_timeout=10) as con:
        return con.cursor(row_factory=dict_row).execute(cast(Any, sql), params).fetchall()  # sql: our own fixed queries


def _value(row: dict) -> Any:
    for key in ('valueString', 'valueDecimal', 'valueInt', 'valueBoolean', 'valueDateTime', 'valueDropdown'):
        value = row.get(key)
        if value is None or value == '':
            continue
        if key == 'valueDropdown' and isinstance(value, str):
            try:
                value = json.loads(value).get('valueDropdown', value)
            except (ValueError, AttributeError):
                pass
        if key == 'valueDecimal':
            value = float(value)
        if key == 'valueDateTime' and isinstance(value, (datetime, date)):
            value = value.isoformat()
        return value
    return None


_DESIGN = '''
select d."designId" as code, d.id as design_id, dv.id as version_id, dv.version, p.name as plant,
       pc.name as classification, ws.name as state, dv."isInvalidated" as invalidated, dv."createdAt" as created
from "Design" d
join "Config" c on c."designId" = d.id
join "Plant" p on p.id = c."plantId"
join "DesignVersion" dv on dv."configId" = c.id
left join "ProductClassification" pc on pc.id = d."productClassificationId"
left join "WorkflowState" ws on ws.id = dv."statusId"
where d."designId" = %s or dv.id::text = %s or d.id::text = %s
order by dv."createdAt" desc
'''


def _design(ref: str) -> dict | None:
    versions = _rows(_DESIGN, (ref, ref, ref))
    if not versions:
        return None
    current = next((v for v in versions if str(v['version_id']) == ref), versions[0])
    vid = current['version_id']
    header = {
        r['name']: v
        for r in _rows(
            'select a.name, v.* from "PlantAttributeValue" v join "PlantAttribute" a on a.id = v."plantAttributeId" '
            'where v."designId" = %s order by a.name',
            (vid,),
        )
        if (v := _value(r)) is not None
    }
    values_by_process: dict[str, dict] = {}
    for r in _rows(
        'select a.name, v.* from "ProcessAttributeValue" v join "ProcessAttribute" a on a.id = v."processAttributeId" '
        'where v."designId" = %s order by a.name',
        (vid,),
    ):
        if (v := _value(r)) is not None:
            values_by_process.setdefault(str(r['processDesignId']), {})[r['name']] = v
    processes = [
        {'process': pd['name'], 'op': pd['op'], 'process_design_id': str(pd['id']), 'values': values_by_process.get(str(pd['id']), {})}
        for pd in _rows(
            'select pd.id, pd."processDesignId" as op, pr.name from "ProcessDesign" pd join "Process" pr on pr.id = pd."processId" '
            'where pd."designId" = %s order by pd."panelOrder" nulls last, pd."processDesignId"',
            (vid,),
        )
    ]
    bom = _rows(
        'select pr.name as process, d."designId" as item from "BOMDesign" b join "ProcessDesign" pd on pd.id = b."processDesignId" '
        'join "Process" pr on pr.id = pd."processId" left join "DesignVersion" dv on dv.id = b."designItemId" '
        'left join "Config" c on c.id = dv."configId" left join "Design" d on d.id = c."designId" where pd."designId" = %s',
        (vid,),
    )
    return {
        'kind': 'design',
        'code': current['code'],
        'design_id': str(current['design_id']),
        'version_id': str(vid),
        'version': current['version'],
        'plant': current['plant'],
        'classification': current['classification'],
        'workflow_state': current['state'],
        'invalidated': current['invalidated'],
        'other_versions': [str(v['version_id']) for v in versions if v['version_id'] != vid][:5],
        'header': header,
        'processes': processes,
        'bom': [f"{b['process']}: {b['item'] or '(material)'}" for b in bom],
    }


_ENQUIRY = '''
select e."enquiryId" as code, e.id as enquiry_id, ev.id as version_id, ev.version, e.priority, cu.name as customer,
       p.name as plant, ws.name as state
from "Enquiry" e
join "EnquiryVersion" ev on ev."enquiryId" = e.id
left join "Customer" cu on cu.id = e."customerId"
left join "Plant" p on p.id = e."plantId"
left join "WorkflowState" ws on ws.id = ev."statusId"
where e."enquiryId" = %s or ev.id::text = %s or e.id::text = %s
order by ev."createdAt" desc
'''


def _enquiry(ref: str) -> dict | None:
    versions = _rows(_ENQUIRY, (ref, ref, ref))
    if not versions:
        return None
    current = next((v for v in versions if str(v['version_id']) == ref), versions[0])
    vid = current['version_id']
    header = {
        r['name']: v
        for r in _rows(
            'select a.name, v.* from "EnquiryAttributeValue" v join "EnquiryAttribute" a on a.id = v."enquiryAttributeId" '
            'where v."enquiryVersionId" = %s order by a.name',
            (vid,),
        )
        if (v := _value(r)) is not None
    }
    lines = _rows(
        'select l.id, l."lineItemId" as line, l.description, l.quantity, d."designId" as design from "LineItem" l '
        'left join "DesignVersion" dv on dv.id = l."designVersionId" left join "Config" c on c.id = dv."configId" '
        'left join "Design" d on d.id = c."designId" where l."enquiryVersionId" = %s order by l."lineItemId"',
        (vid,),
    )
    files = _rows('select "fileName" from "EnquiryVersionFile" where "enquiryVersionId" = %s', (vid,))
    return {
        'kind': 'enquiry',
        'code': current['code'],
        'enquiry_id': str(current['enquiry_id']),
        'version_id': str(vid),
        'customer': current['customer'],
        'plant': current['plant'],
        'priority': current['priority'],
        'workflow_state': current['state'],
        'header': header,
        'line_items': [
            {'id': str(l['id']), 'line': l['line'], 'description': l['description'], 'quantity': float(l['quantity']) if l['quantity'] is not None else None, 'design': l['design']}
            for l in lines
        ],
        'files': [f['fileName'] for f in files],
    }


def _catalogue(ref: str) -> dict | None:
    found = _rows(
        'select c.id, c."catalogueName" as name, c.description, c."formulaExecutionMode" as mode, p.name as plant '
        'from "Catalogue" c left join "Plant" p on p.id = c."plantId" where c.id::text = %s or c."catalogueName" = %s',
        (ref, ref),
    )
    if not found:
        return None
    cat = found[0]
    # Catalogue <-> Config is Prisma's implicit many-to-many table: A = Catalogue, B = Config.
    designs = _rows(
        'select d."designId" as code from "_ConfigToCatalogue" j join "Config" cf on cf.id = j."B" '
        'join "Design" d on d.id = cf."designId" where j."A" = %s order by d."designId"',
        (cat['id'],),
    )
    return {
        'kind': 'catalogue',
        'code': cat['name'],
        'catalogue_id': str(cat['id']),
        'plant': cat['plant'],
        'formula_execution_mode': cat['mode'],
        'description': cat['description'],
        'design_count': len(designs),
        'designs': [d['code'] for d in designs][:100],
    }


def values_for(codes: list[str], fields: list[str]) -> dict[str, dict]:
    """Chosen attribute values of designs, by code, from each design's current version: plant (header) attributes
    by name, process attributes as PROCESS.attribute (e.g. INSULATING_MV.nomThickness)."""
    # Refs may be codes, design ids or version ids; a version id selects that version, the others the latest.
    versions = _rows(
        'select distinct on (d."designId") d."designId" as code, dv.id, d.id::text as did from "Design" d join "Config" c on c."designId" = d.id '
        'join "DesignVersion" dv on dv."configId" = c.id '
        'where d."designId" = any(%s) or d.id::text = any(%s) or dv.id::text = any(%s) '
        'order by d."designId", (dv.id::text = any(%s)) desc, dv."createdAt" desc',
        (codes, codes, codes, codes))
    by_version = {r['id']: r['code'] for r in versions}
    out: dict[str, dict] = {r['code']: {} for r in versions}
    plain = [f for f in fields if '.' not in f]
    process = [tuple(f.split('.', 1)) for f in fields if '.' in f]
    if plain and by_version:
        for r in _rows('select v."designId", a.name, v.* from "PlantAttributeValue" v join "PlantAttribute" a on a.id = v."plantAttributeId" '
                       'where v."designId" = any(%s) and a.name = any(%s)', (list(by_version), plain)):
            out[by_version[r['designId']]][r['name']] = _value(r)
    if process and by_version:
        for r in _rows('select pr.name as process, a.name, v.* from "ProcessAttributeValue" v '
                       'join "ProcessAttribute" a on a.id = v."processAttributeId" join "Process" pr on pr.id = a."processId" '
                       'where v."designId" = any(%s) and pr.name = any(%s) and a.name = any(%s)',
                       (list(by_version), [p for p, _ in process], [a for _, a in process])):
            key = f"{r['process']}.{r['name']}"
            if key in fields:
                out[by_version[r['designId']]][key] = _value(r)
    for r in versions:  # findable by code, design id or version id
        out[str(r['id'])] = out[r['did']] = out[r['code']]
    return out


_names: tuple[float, set[str], dict[str, set[str]]] | None = None


def attribute_names() -> tuple[set[str], dict[str, set[str]]]:
    """Design attribute names: plant (header) names, and process attribute name -> processes that have it.
    Lets a search that matched nothing say a filter's field does not exist. Cached for 30 minutes."""
    global _names
    if _names is None or time.monotonic() - _names[0] > 1800:
        plant = {r['name'] for r in _rows('select distinct name from "PlantAttribute"')}
        process: dict[str, set[str]] = {}
        for r in _rows('select distinct a.name, pr.name as process from "ProcessAttribute" a join "Process" pr on pr.id = a."processId"'):
            process.setdefault(r['name'], set()).add(r['process'])
        _names = (time.monotonic(), plant, process)
    return _names[1], _names[2]


async def get(kind: str, ref: str) -> dict | None:
    loader = {'design': _design, 'enquiry': _enquiry, 'catalogue': _catalogue}.get(kind)
    if not loader:
        return None
    return await asyncio.to_thread(loader, str(ref).strip())


async def find_any(ref: str) -> dict | None:
    """A design, enquiry or catalogue by code/name or id, whichever exists."""
    for kind in ('design', 'enquiry', 'catalogue'):
        record = await get(kind, ref)
        if record:
            return record
    return None


def clip(record: dict) -> str:
    text = json.dumps(record, ensure_ascii=False, default=str)
    if len(text) <= MAX_CHARS:
        return text
    # Too big: keep identity and header, summarize processes.
    slim = {**record, 'processes': [{'process': p['process'], 'op': p['op'], 'values': f"{len(p['values'])} values (cc_get with process= to see)"} for p in record.get('processes', [])]}
    return json.dumps(slim, ensure_ascii=False, default=str)[:MAX_CHARS]


def ids_in(value: Any, limit: int = 6) -> list[str]:
    """Record references in an API response or path: design/enquiry codes and UUIDs."""
    found: list[str] = []

    def walk(node: Any, key: str = '') -> None:
        if len(found) >= limit:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node[:20]:
                walk(v, key)
        elif isinstance(node, str):
            if _UUID.match(node) and key.lower() in ('id', 'designid', 'designversionid', 'versionid', 'newdesignid', 'enquiryid', 'enquiryversionid', 'design_id', 'version_id', 'catalogueid', 'catalogue_id'):
                found.append(node)
            elif re.match(r'^(ENG-)?[A-Z]{2,4}-\d{3,6}$|^ENQ-\d{3,6}$', node):
                found.append(node)

    walk(value)
    return list(dict.fromkeys(found))


if __name__ == '__main__':
    assert _value({'valueDropdown': '{"valueDropdown":"300"}'}) == '300'
    assert _value({'valueString': '', 'valueInt': 4}) == 4
    assert ids_in({'data': {'designId': 'LV-7041', 'id': '920ff075-f8d0-4b7c-9d8a-258ddb5a91be'}}) == ['LV-7041', '920ff075-f8d0-4b7c-9d8a-258ddb5a91be']
    print('records self-check OK')
