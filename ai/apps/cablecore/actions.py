"""cc_action: validate -> dry-run -> one approval for the exact changes -> run -> follow jobs -> read back.

Every change is logged in the turn (`ctx.writes`), with the record as it is afterwards, so the after-turn
check compares the reply with what the data now says, not with what was intended.
"""

import asyncio
import json
import re
import time
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from ai import sandbox
from ai.apps.cablecore import api, index, records
from ai.context import ApprovalCard, ApprovalItem, TurnContext, WriteRecord


class Action(BaseModel):
    """One cableCORE API call in a cc_action batch."""

    model_config = ConfigDict(extra='ignore')

    id: str = Field(validation_alias=AliasChoices('id', 'name'), description='the route id (from cc_find or the catalogue)')
    path: dict[str, Any] | None = Field(None, description='path parameters')
    query: dict[str, Any] | None = Field(None, description='query parameters')
    body: Any = Field(None, description='JSON body (form fields for an upload)')
    file: str | None = Field(None, description='sandbox path of a file to upload, for routes that take one')

JOB_TIMEOUT_S = 120
RESULT_CHARS = 3000

def _clip(value: Any, limit: int = RESULT_CHARS) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + f'… ({len(text) - limit} more chars)'


def _readable(value: Any) -> str:
    # Dropdown values travel as '{"valueDropdown":"300"}'; the approval card shows 300.
    text = json.dumps(value, ensure_ascii=False, default=str)
    return re.sub(r'"\{\\"valueDropdown\\":\\"([^"\\]*)\\"\}"', r'"\1"', text)


def _dig(data: Any, dotted: str) -> Any:
    for part in dotted.split('.'):
        if isinstance(data, dict):
            data = data.get(part)
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return None
    return data


_REF = re.compile(r'^\$(\d+)(?:\.(.+))?$')


def _has_refs(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_REF.match(value))
    if isinstance(value, dict):
        return any(_has_refs(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_refs(v) for v in value)
    return False


def _fill_refs(value: Any, outputs: list[Any]) -> Any:
    """"$2.data.versionId" -> that field of action 2's (unwrapped) result, so one batch can chain."""
    if isinstance(value, str) and (m := _REF.match(value)):
        n, dotted = int(m.group(1)), m.group(2)
        if not 1 <= n <= len(outputs):
            raise api.CableCoreError(f'{value}: action {n} has not run yet.')
        dotted = re.sub(r'\[(\d+)\]', lambda g: '.' + g.group(1), dotted).strip('.') if dotted else dotted  # data[0].id == data.0.id
        found = _dig(outputs[n - 1], dotted) if dotted else outputs[n - 1]
        if found is None:
            raise api.CableCoreError(f'{value}: action {n} returned no {dotted!r} (result: {_clip(outputs[n - 1], 400)})')
        return found
    if isinstance(value, dict):
        return {k: _fill_refs(v, outputs) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill_refs(v, outputs) for v in value]
    return value


def _file_summary(file: api.FileResult) -> dict:
    """What is in a returned file, so an empty export is visible: rows per sheet and the header row."""
    name = file.name.lower()
    try:
        if name.endswith(('.xlsx', '.xlsm')):
            import io

            import openpyxl

            book = openpyxl.load_workbook(io.BytesIO(file.content), read_only=True)
            sheets = {}
            for sheet in book.worksheets[:5]:
                rows = list(sheet.iter_rows(max_row=2, values_only=True))
                sheets[sheet.title] = {
                    'data_rows': max((sheet.max_row or 1) - 1, 0),
                    'columns': [c for c in (rows[0] if rows else []) if c is not None][:15],
                }
            return {'sheets': sheets}
        if name.endswith(('.csv', '.tsv', '.txt')):
            lines = file.content.decode(errors='replace').splitlines()
            return {'data_rows': max(len(lines) - 1, 0), 'columns': lines[0][:300] if lines else ''}
    except Exception as e:
        return {'summary_error': str(e)[:200]}
    return {}


async def _follow_job(token: str, route: index.Route, data: Any) -> dict | None:
    job = (route.capability or {}).get('job')
    if not job:
        return None
    job_id = _dig(api.unwrap(data), job['idPath'])
    if not job_id:
        return None
    status_route = index.get(job['status'])
    if not status_route:
        return {'job_id': job_id, 'state': 'started', 'status_action': job['status']}
    deadline = time.time() + JOB_TIMEOUT_S
    state, body = None, None
    while time.time() < deadline:
        status, body = await api.call('GET', index.fill_path(status_route, {job['idParam']: job_id}), token)
        state = _dig(api.unwrap(body), job['statusPath'])
        if state in job.get('done', []) or state in job.get('failed', []):
            break
        await asyncio.sleep(2)
    return {
        'job_id': job_id,
        'state': state,
        'finished': state in job.get('done', []),
        'failed': state in job.get('failed', []),
        'status': _clip(api.unwrap(body), 800),
        **({} if state in job.get('done', []) + job.get('failed', []) else {'status_action': job['status'], 'note': f'still running after {JOB_TIMEOUT_S}s'}),
    }


async def run(actions: list[Action] | list[dict[str, Any]], verify: list[str] | None, ctx: TurnContext) -> str:
    token = ctx.cc_token
    await index.ensure(token)

    planned: list[tuple[index.Route, dict[str, Any], dict[str, Any], Any, str | None]] = []
    problems: list[str] = []
    for n, raw in enumerate(actions or [], 1):
        action = raw if isinstance(raw, Action) else Action.model_validate(raw)
        rid = action.id
        route = index.get(rid)
        if not route:
            near = '; '.join(r.id for r in index.find(rid.replace('_', ' ').replace('.', ' '), 3))
            problems.append(f'action {n}: no route {rid!r}. Closest: {near}. Use cc_find.')
            continue
        path, query, body = action.path or {}, action.query or {}, action.body
        errors = index.validate(route, path, query, body)
        if action.file and not route.file_field:
            errors.append('file: this route takes no file upload; find the route that uploads it (cc_find "upload file ...")')
        if action.file is None and route.file_field and (route.capability or {}).get('input', {}).get('file'):
            errors.append(f"file: this route needs file=<sandbox path> (field '{route.file_field}')")
        problems += [f'action {n} ({rid}): {e}' for e in errors]
        planned.append((route, path, query, body, action.file))
    if problems:
        return json.dumps({'status': 'invalid', 'nothing_ran': True, 'problems': problems}, ensure_ascii=False)

    # The backend's own dry-run for routes that link one (e.g. attribute updates).
    for route, path, query, body, _ in planned:
        check_name = (route.capability or {}).get('validate')
        check = index.get(check_name) if check_name else None
        # An action fed by an earlier one's result ($1...) can only be checked when it runs.
        if check and check.risk == 'read' and not _has_refs([path, query, body]):
            status, data = await api.call(check.method, index.fill_path(check, path), token, query=query, body=body)
            if error := api.failed(status, data):
                return json.dumps({'status': 'invalid', 'nothing_ran': True, 'problems': [f'{route.id}: {check.id} says {error}']}, ensure_ascii=False)

    writes = [p for p in planned if p[0].risk != 'read']
    if writes:
        items: list[ApprovalItem] = []
        for route, path, query, body, file in writes:
            args = {k: v for k, v in (('path', path), ('query', query), ('body', body), ('file', file)) if v}
            items.append(ApprovalItem(
                summary=route.summary.split('. ')[0][:120].replace('*', ''),
                method=route.method,
                path=index.fill_path(route, path),
                args=_readable(args)[:600].replace('`', ''),
            ))
        markdown = '\n\n'.join(f'{i}. **{it.summary}**  \n`{it.method} {it.path}`  \n`{it.args}`' for i, it in enumerate(items, 1))
        answer = await ctx.ask(ApprovalCard(
            title=f"Approve {len(writes)} change{'s' if len(writes) > 1 else ''} in cableCORE", items=items, markdown=markdown,
        ))
        if answer.approved is not True:
            return json.dumps({'status': 'declined', 'nothing_ran': True, 'note': 'The user did not approve these changes.'})

    results = []
    outputs: list[Any] = []
    stopped = False
    for route, path, query, body, file in planned:
        if stopped:
            results.append({'action': route.id, 'status': 'not run (an earlier action failed)'})
            outputs.append(None)
            continue
        try:
            path, query, body = _fill_refs(path, outputs), _fill_refs(query, outputs), _fill_refs(body, outputs)
        except api.CableCoreError as e:
            results.append({'action': route.id, 'status': 'failed', 'error': str(e)})
            outputs.append(None)
            stopped = True
            continue
        url_path = index.fill_path(route, path)
        upload = await sandbox.read_file(file) if file else None
        status, data = await api.call(
            route.method, url_path, token, query=query,
            body=None if upload else body,
            file=upload, form=body if upload and isinstance(body, dict) else None,
            file_field=route.file_field or 'file',
        )
        if isinstance(data, api.FileResult):
            # The route returned a file: store it in the sandbox and hand back its download link.
            stored = await sandbox.save_file(data.name, data.content, data.mime)
            data = {
                'file': data.name,
                'bytes': len(data.content),
                'path': stored,
                'download_link': f'/sandbox{stored}',
                **await asyncio.to_thread(_file_summary, data),
            }
        error = api.failed(status, data)
        outputs.append(None if error else api.unwrap(data))
        entry: dict[str, Any] = {'action': route.id, 'status': 'failed' if error else 'done'}
        if error:
            entry['error'] = error
            stopped = route.risk != 'read'
        else:
            entry['result'] = _clip(api.unwrap(data))
            if job := await _follow_job(token, route, data):
                entry['job'] = job
                if job.get('failed'):
                    entry['status'] = 'failed'
                    stopped = True
        if route.risk != 'read':
            entry['_refs'] = records.ids_in({'path': path, 'response': api.unwrap(data)})
        results.append(entry)

    # Read back what changed: records named in paths and responses, and those the model asked for.
    refs = list(dict.fromkeys([*(verify or []), *[r for e in results for r in e.pop('_refs', [])]]))[:4]
    after = {}  # what the model gets: short
    full_after = {}  # what the completion check gets: every value, so a change it judges is in view
    for ref in refs:
        record = await records.find_any(str(ref))
        if record:
            key = f"{record['kind']} {record['code']}"
            full_after[key] = record
            clipped = records.clip(record)
            after[key] = json.loads(clipped) if len(clipped) < 4000 else clipped[:4000]

    if writes:
        # The whole batch, reads included (an export, a job's final state), so the check sees what happened.
        ledger = ctx.writes
        for entry, (route, path, query, body, file) in zip(results, planned):
            ledger.append(WriteRecord(
                at=time.time(), action=route.id, risk=route.risk, method=route.method,
                path=index.fill_path(route, path), query=query,
                request=_clip({'body': body, 'file': file}, 2000), status=entry['status'],
                response=entry.get('error') or _clip({'result': entry.get('result', ''), 'job': entry.get('job')}, 3000),
            ))
        if ledger:
            ledger[-1].after = _clip(full_after, 30000)

    return json.dumps({'results': results, 'after': after}, ensure_ascii=False, default=str)
