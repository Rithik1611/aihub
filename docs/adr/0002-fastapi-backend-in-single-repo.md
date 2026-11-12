# 0002. FastAPI backend in a single repo

Status: Accepted
Date: 2026-11-12

## Context

aihub needs an HTTP API. The repo also holds `ai/`, and a frontend is likely later.
The team is Python-first and wants typed request/response models and generated
API docs without writing a schema by hand.

## Decision

One git repository at the project root, with the API as a FastAPI app in `backend/`.
Inside it: `app/main.py` wires the app, `app/config.py` reads settings from the
environment, `app/routers/` holds one module per resource group, all mounted under
the `/api` prefix.

## Consequences

Pydantic gives validation and OpenAPI docs for free, and other components can live
beside the backend without cross-repo version skew. One repo means one CI pipeline
that must learn to only run what changed. Routers are flat for now; if a resource
grows its own services and schemas, it becomes a package without moving the rest.
Splitting `backend/` out later is a subtree extract, not a rewrite.
