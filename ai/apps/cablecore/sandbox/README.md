# cableCORE sandbox

Shared Linux workspace (Python 3.12, pandas, openpyxl, requests, psycopg, psql, git, sudo).
Several users' chats run here, so keep each user's files under `~/work/<user email>/`. This file and the schema are under `~/cablecore/` (read-only).

## What can be reached from here

| What | How |
|---|---|
| cableCORE database (read-only) | `$CC_DB_URL` — `psql "$CC_DB_URL"` or `psycopg.connect(os.environ["CC_DB_URL"])`. Role `ai_reader`: SELECT only, 30 s statement timeout. |
| Database schema | `~/cablecore/schema/*.prisma` (one file per domain). Table and column names in SQL are the Prisma model/field names, quoted: `"ProcessDesign"."designId"`; `@@map`/`@map` override them. |
| cableCORE REST API | `$CC_API_BASE/<route>`, e.g. `$CC_API_BASE/account/profile`, with header `X-CC-Ticket: <ticket>`. Get a ticket from the `cablecore_api_access` tool. A read ticket allows GET; changes need a write ticket, which the user approves. Responses are the backend's own JSON (usually `{statusCode, message, data}`). |
| API reference | `$CC_API_BASE/openapi.json` (needs a ticket) — every route with its parameters and body schema. Operations with an `operationId` like `design.copy` carry a written description. |
| Backend / frontend source search | Zoekt: `curl -s $ZOEKT_URL/api/search -d '{"Q":"repo:cableCORE_BE validateDropdown","Opts":{"MaxDocDisplayCount":10,"NumContextLines":3}}'`. Repos: cableCORE_BE (NestJS), cableCORE_FE (Next.js). |
