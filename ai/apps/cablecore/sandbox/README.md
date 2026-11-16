# cableCORE sandbox

Linux workspace (Python 3.12, pandas, openpyxl, requests, git). Each user has their own Linux account and home
folder (`~`); other users' files are not readable. Keep your files under `~/work/`. This file, the helpers
(`cc.py`) and the schema are in `/opt/cablecore/` (read-only).

There is no database login here: cableCORE data is read through the backend, with the user's own licence and
plants (`import cc`).

## What can be reached from here

| What | How |
|---|---|
| Table rows | `cc.API(ticket).rows('DesignHistory', record='LV-7035')`: `{total, more, rows}`; arguments as the assistant's `cc_related` (record, where, columns, order_by, limit <= 500, offset, via, plant), `all_pages=True` for every page. |
| Database schema | `/opt/cablecore/schema/*.prisma` (one file per domain): table (model) and field names for `rows`. |
| cableCORE REST API | `cc.API(ticket).get(path)` / `.post(path, body)`, i.e. `$CC_API_BASE/<route>` with header `X-CC-Ticket: <ticket>`. Get a ticket from the `cablecore_api_access` tool. Reads only; changes go through the assistant's `cc_action`. |
| API reference | `cc.API(ticket).routes('text')`, or `$CC_API_BASE/openapi.json` with a ticket. |
| Backend / frontend source search | `cc.code('validateDropdown', repo='cableCORE_BE')`. Repos: cableCORE_BE (NestJS), cableCORE_FE (Next.js). |
