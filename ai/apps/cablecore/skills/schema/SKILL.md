---
name: schema
description: Which Prisma schema file holds which models, for SQL in the sandbox (cc.sql) or reading a model's exact fields. Load before writing SQL.
---
# Schema files

In the sandbox at `~/cablecore/schema/`. SQL names are the quoted Prisma names (`select * from "DesignVersion"`); a model with `@@map` uses the mapped table name.

{{SCHEMA_INDEX}}
