# 0006. aihub authenticates with cableCORE tokens and licence claims

Status: Accepted; CSRF part superseded by 0007
Date: 2026-11-13

## Context

aihub is used from inside cableCORE, not as a separate application, so it must not
have its own login. It verifies the access token cableCORE issues, using the shared
`JWT_SECRET` (HS256), read from an `Authorization: Bearer` header or the
`access_token` cookie.

Verifying the signature is not the same as authorising the request, and a review
found the difference mattered:

- **cableAI is a licensed feature.** cableCORE's `assertCableAiAccess` requires an
  active plant and `canAccessAi` (the `cableAI` module on that plant, or Admin plus
  `company.hasCableAI`). aihub enforced nothing. Measured against the running system:
  account `DCAdmin`, who has no cableAI licence, got `403` from cableCORE and `200`
  from aihub, and the message was stored. An unlicensed user had full access to a paid
  feature.
- **Account state is invisible to aihub.** cableCORE's JWT strategy re-reads the
  account on every request and rejects tokens for accounts that are inactive, deleted,
  signed out, signed in elsewhere, or idle-timed-out. aihub has no access to that
  table, so a revoked session kept working until the token expired - up to an hour.
- **Cookie authentication had no CSRF defence.** cableCORE runs `csurf` for cookie
  flows and skips it for Bearer. aihub accepted the cookie unconditionally and was
  protected only as a side effect of CORS preflight and JSON-only body parsing.
- No per-user rate limit, where cableCORE enforces 60/minute, on endpoints that spend
  money calling an LLM.

Two ways to get the licence were considered. Calling cableCORE's `POST /mcp/whoami`
per request would give the licence *and* the live account state, since that route runs
the full session checks - it exists for the AI engine to do exactly this. Carrying the
licence as JWT claims avoids the network hop and the runtime dependency.

## Decision

cableCORE adds `activePlantId` and `canAccessAi` to the access-token payload. aihub
requires both on every chat route: a missing or false `canAccessAi`, or an absent
active plant, is a `403` with cableCORE's own wording.

Cookie-authenticated state-changing requests must carry a valid `X-CSRF-Token`,
verified against the `_csrf` secret cookie using csurf's own scheme
(`salt + "-" + base64url(sha1(salt + "-" + secret))`), confirmed against a live token.
Bearer requests skip it, as they do in cableCORE.

Chat routes are rate limited per account.

## Consequences

The licence bypass is closed and no runtime dependency on cableCORE is added: a token
is self-contained, so chat keeps working if cableCORE is briefly unreachable.

The claims are a snapshot. They are refreshed only when a token is issued, so for up
to the access-token lifetime (one hour by default) aihub can be wrong about a user:

- `changeActivePlant` updates the database **without re-issuing the token**, so a user
  who switches into a licensed plant stays blocked, and one who switches out keeps
  access. cableCORE itself reads this value from the primary specifically to avoid
  serving it stale, so the risk is one it already recognises.
- Account state is still not revalidated. Deactivation, deletion and sign-out do not
  reach aihub until the token expires.

Both windows close if `changeActivePlant` re-issues tokens and access-token lifetime
stays short. If either turns out to matter more than the network hop, the way back is
`POST /mcp/whoami`, which answers both questions at once - and this ADR gets
superseded rather than patched.

Requiring the claims means tokens issued before this change are refused on chat
routes. cableCORE must be deployed first, and users with a token in flight get a 403
until they next sign in or refresh.
