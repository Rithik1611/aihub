---
name: sandbox-analysis
description: Calculations, bulk analysis, reading attached files and making files in the sandbox (Linux, Python 3.12, pandas), with read-only access to cableCORE data from code. Load before using the sandbox.
---
# The sandbox

Linux with Python 3.12, pandas, numpy, openpyxl. For calculations, analysis, chart data, reading attached files and producing files.

`import cc` gives read-only data access:
- `cc.sql(query)`: the read-only database, rows as dicts (table names: the `schema` skill).
- `cc.API(ticket).get(path)` / `.post(search_path, body)`: the cableCORE API, with a ticket from `cablecore_api_access`.
- `cc.code('text', repo='cableCORE_BE')`: search the backend's source, e.g. to see how a route behaves.

`run_command` takes `wait` (seconds, up to 300) and returns when the command finishes.
Your files go in `~/work/{{USER_EMAIL}}/`; other users share the machine. Changes to cableCORE data are never made from the sandbox: use `cc_action`.
