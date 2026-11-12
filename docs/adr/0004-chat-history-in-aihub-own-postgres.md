# 0004. Normal-chat history lives in aihub on its own Postgres

Status: Accepted
Date: 2026-11-12

## Context

cableCORE_BE stores all chat history in its application database: `ChatSession` and
`ChatMessage` in `prisma/models/system.prisma`, reached only through
`src/database/chat.db.service.ts`. One code path serves every mode — DEFAULT, SUPPORT
and FORMULA chat share the same two tables and the same persistence methods, and differ
only by the `mode` column and which AI endpoint is called.

aihub is being built to serve several applications, not just cableCORE. Keeping another
application's conversations inside cableCORE's database would make cableCORE the de
facto owner of data it has no stake in, and would couple every future consumer to a
cableCORE schema migration.

Normal chat (`mode = DEFAULT`) is also moving its entry point: requests will go from the
frontend straight to aihub, which calls the AI service inside aihub. cableCORE_BE is not
in that path. SUPPORT and FORMULA chat stay in cableCORE_BE, served and stored there.

Alternatives considered for the store: MongoDB (session-as-document makes the
edit-and-regenerate rewrite a single atomic update, but caps transcripts at the 16MB
document limit and weakens the session-list query), DynamoDB and Cassandra (range delete
plus transactional rewrite is a poor fit for both), ClickHouse (not an OLTP store),
Redis and SQLite (not a durable multi-app primary store).

## Decision

Normal-chat history moves to aihub, in a **separate PostgreSQL database** with no
connection to cableCORE's. aihub owns the DEFAULT-mode chat request end to end:
persistence, and the call to the aihub AI service.

Postgres because the edit-and-regenerate flow (`rewriteFromMessage`) deletes a range of
messages, updates one and inserts another, and all three must be atomic — free in
Postgres, awkward in every alternative except MongoDB. JSONB covers message metadata,
and the three existing indexes port unchanged. Volume is small, so no alternative is
justified by scale.

## Consequences

The port is a 1:1 schema translation, which keeps semantics stable while the language
and framework change. aihub can serve other applications without touching cableCORE.

Three guarantees do not survive the split and must be rebuilt explicitly:

- `Account.lastActiveSessionId` is a column on cableCORE's `Account` table. It becomes
  an aihub-side record keyed by application and user.
- `ChatSession.accountId` is a foreign key with `onDelete: Cascade`. Across databases
  that cascade is gone: deleting a user in cableCORE will not delete their aihub chat
  history. Retention needs its own mechanism.
- There is no tenant dimension, and none is added now: aihub is used from within
  cableCORE, not as a standalone application. When aihub grows to serve other
  applications it will do so through an OAuth connector, and that is when a tenant
  identifier earns its place. Adding one today would be a column on every row and a
  predicate in every query, paid for before there is a second application.

aihub becomes a trust boundary of its own: it now authenticates requests cableCORE used
to authenticate. It verifies cableCORE-issued tokens with the shared `JWT_SECRET`
(HS256), read from an `Authorization: Bearer` header or the `access_token` cookie, and
refuses anything that is not an `access` token.

Two limits come with that, and neither is hidden:

- cableCORE's JWT strategy also checks the account row on every request - `isActive`,
  `isDeleted`, a live refresh token, session replacement and idle timeout. aihub cannot
  see that table, so it verifies only what the token itself proves: signature, expiry,
  type and subject. A deactivated or signed-out user's token keeps working in aihub
  until it expires. Short access-token lifetimes are what bounds this.
- A symmetric secret shared between two services means aihub can mint cableCORE tokens,
  not just verify them. An asymmetric key (cableCORE signs, aihub verifies a public key)
  removes that, and is the natural shape for the OAuth connector above. Worth revisiting
  then rather than now.

Two chat histories now exist — DEFAULT in aihub, SUPPORT and FORMULA in cableCORE. Any
feature that needs a single view across modes has to join across two services. If that
becomes a requirement, this ADR gets superseded rather than amended.
