# 0007. CSRF protection by exact Origin and JSON-only writes, not a token

Status: Accepted
Date: 2026-11-13
Supersedes: the CSRF part of 0006

## Context

aihub accepts cableCORE's `access_token` cookie, and a browser attaches that cookie
to any request it sends to aihub - including one started by another site. Cookie
authentication therefore needs CSRF protection.

0006 mirrored cableCORE: verify an `X-CSRF-Token` against csurf's `_csrf` secret
cookie. That turned out to be both broken and weak for aihub:

- **Broken in the browser.** axios 1.6+ does not send its XSRF header cross-origin
  unless `withXSRFToken` is set, and cableCORE's frontend never sets it. The
  frontend (`:3001`) and aihub (`:8010`) are different origins, so every browser
  write to aihub would have been refused. Tests passed only because they set the
  header by hand.
- **Weak.** csurf's token is an unkeyed SHA-1 of a secret held in a cookie. Anyone
  who can set a cookie on the parent domain (a compromised or hostile sibling
  subdomain) can plant both halves and pass the check.
- **Coupled.** It depended on cableCORE's `_csrf` cookie being sent to aihub, so a
  cookie-attribute change in cableCORE could silently break aihub.

## Decision

For a cookie-authenticated request with an unsafe method, aihub requires:

1. an `Origin` header that exactly matches an entry in `CORS_ORIGINS` - scheme,
   host and port. Missing or `null` is refused. A page cannot set or forge
   `Origin`; the browser does.
2. for POST, `Content-Type: application/json`. That type is not CORS-safelisted,
   so no other site can send it without a preflight aihub refuses. The safelisted
   types an attacker could use without a preflight - form posts, `text/plain`,
   multipart - are refused outright rather than relying on the body failing to parse.
   PATCH and DELETE always preflight, so they need only the Origin check.

Bearer requests are exempt: the browser never attaches an `Authorization` header on
its own, so it cannot be used cross-site.

`CORS_ORIGINS` is the single allowlist for both CORS and these checks. It is
validated at startup: each entry must be exactly `scheme://host[:port]`, with no
wildcard and no path. CORS lists methods and headers explicitly rather than `*`.

## Consequences

Nothing for the client to fetch, store or send, and no secret to share - it works
with the existing frontend unchanged, and it no longer depends on any cableCORE
cookie. Every guarantee rests on behaviour browsers enforce: `Origin` and preflight.

That is the trade-off. It assumes a browser that sends `Origin` on cross-origin
writes, which every current browser does; a client that does not is refused rather
than trusted. Non-browser callers must use a Bearer token. Adding an origin to
`CORS_ORIGINS` now grants write access as well as read access, so that list is a
security control and should be reviewed like one.
