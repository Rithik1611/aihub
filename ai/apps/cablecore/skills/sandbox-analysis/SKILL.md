---
name: sandbox-analysis
description: Calculations, bulk analysis, reading attached files and making files in the sandbox (Linux, Python 3.12, pandas), with read-only access to cableCORE data from code. Load before using the sandbox.
---
# The sandbox

Linux with Python 3.12, pandas, numpy, openpyxl. For calculations, analysis, chart data, reading attached files and producing files.

`import cc` gives read-only data access:
- `cc.API(ticket).rows(table, record=..., where=..., all_pages=True)`: table rows as dicts, the same arguments and rules as `cc_related` (table names: the `schema` skill). There is no SQL access.
- `cc.API(ticket).get(path)` / `.post(search_path, body)`: the cableCORE API. Both take a ticket from `cablecore_api_access`.
- `cc.code('text', repo='cableCORE_BE')`: search the backend's source, e.g. to see how a route behaves.

`run_command` takes `wait` (seconds, up to 300) and returns when the command finishes.
Your files go in `~/work/` (this user's own home; other users cannot read it). Changes to cableCORE data are never made from the sandbox: use `cc_action`.
