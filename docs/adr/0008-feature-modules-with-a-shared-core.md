# 0008. Feature modules with a shared core

Status: Accepted
Date: 2026-11-13

## Context

The backend started as a flat `app/`: `config.py`, `db.py`, `auth.py` and
`rate_limit.py` beside a `routers/` folder, with chat split across `app/chat/` for
its data and `app/routers/chat.py` for its endpoints. One feature, so one layer of
files was enough.

It is no longer one feature. Chat is being hardened module by module, and more will
follow. With endpoints in one tree and everything else in another, "the chat module"
was not a thing you could point at, review or raise the quality of on its own.

## Decision

Each feature is a package under `app/`, owning everything it needs and exposing a
`router`:

```
app/chat/      router, schemas, models, repository, metadata, ai_client
app/health/    router
```

Shared infrastructure lives in `app/core/`: configuration, database session,
authentication and rate limiting. It owns no business rules.

The dependency rule is one-way: **feature modules import from `app.core`; `app.core`
never imports from a feature module.** `app/main.py` only wires modules together -
one `include_router` per feature.

## Consequences

A module is now a directory: it can be reviewed, tested and hardened on its own, and
what it depends on is visible in its imports. Adding a feature means adding a package
and one line in `main.py`, with no shared router registry to edit.

The one-way rule is what keeps that true, and nothing enforces it yet - a `core`
module importing from `app.chat` would compile. If that starts happening, an import
lint rule is the answer.

This was a pure move: no behaviour changed, verified by the suite and by the live
endpoints before and after. Two features do not need this structure on their own;
it is here because the next ones will.
