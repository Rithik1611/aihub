# Architecture Decision Records

One file per decision, newest number wins. Format: [MADR](https://adr.github.io/madr/)-lite.

Write an ADR when a choice is hard to reverse or someone will ask "why is it like this?":
a framework, a dependency manager, a datastore, an auth model, an API shape, a repo layout.
Skip it for anything a reader can see from the code.

New one: copy `template.md` to `NNNN-short-title.md`, next number, add a row below.
Superseding an old decision: write a new ADR, set the old one's status to
`Superseded by NNNN` — never rewrite history.

| # | Decision | Status |
|---|----------|--------|
| [0001](0001-record-decisions-in-adrs.md) | Record decisions in ADRs | Accepted |
| [0002](0002-fastapi-backend-in-single-repo.md) | FastAPI backend in a single repo | Accepted |
| [0003](0003-uv-for-dependency-management.md) | uv for dependency management | Accepted |
| [0004](0004-chat-history-in-aihub-own-postgres.md) | Normal-chat history in aihub on its own Postgres | Accepted |
