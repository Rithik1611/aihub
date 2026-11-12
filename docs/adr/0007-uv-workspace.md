# 0007. One uv workspace for the agent and the backend

Status: Proposed
Date: 2026-11-13

## Context

ADR 0003 put the backend on uv with its own `pyproject.toml` and `uv.lock`. The agent core (`ai/`) is
now a second Python package in the repo, and the backend imports it (ADR 0006). Two locks would let
the shared libraries (pydantic, httpx, openai) drift apart between the two.

## Decision

A uv workspace at the repo root. The root `pyproject.toml` is the agent package (`aihub-ai`, the `ai/`
folder); `backend/` is a member that keeps its own `pyproject.toml` and depends on `aihub-ai` from the
workspace. One `uv.lock` and one `.venv` at the root; Python 3.12 (`.python-version`).

## Consequences

`uv sync --all-packages` at the root sets up both; `backend/uv.lock` is gone (ADR 0003's committed lock
is now the root one). Each package keeps its own checks: mypy for `backend/`, pyright and ruff for `ai/`.
Splitting the backend out later means giving it its own lock again and depending on `aihub-ai` by version.
