# 0003. uv for dependency management

Status: Accepted
Date: 2026-11-12

## Context

The backend started with `pip` and a `requirements.txt`, which pins direct
dependencies but not the full resolved tree, so environments drift between
machines and CI. Virtualenv creation, activation and install were three manual
steps.

## Decision

Manage dependencies with [uv](https://docs.astral.sh/uv/): `pyproject.toml` for
declared dependencies (runtime, plus a `dev` group), `uv.lock` committed for the
exact resolved tree. `uv sync` sets up the environment; `uv run` executes commands
in it. `requirements.txt` was removed.

## Consequences

Setup is one command and everyone gets byte-identical dependencies. Installs are
much faster, which CI feels most. uv must be installed on every machine and build
image — it is a single binary, and `uv export` can regenerate a `requirements.txt`
for anything that insists on pip. uv is young, so a breaking change in it is a risk
we accept; the `pyproject.toml` it reads is standard and portable if we leave.
