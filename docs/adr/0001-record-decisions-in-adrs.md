# 0001. Record decisions in ADRs

Status: Accepted
Date: 2026-11-12

## Context

The project is starting from an empty repo. Early choices (framework, tooling,
layout) are the ones most likely to be questioned later, and the reasoning behind
them lives only in chat logs and people's heads.

## Decision

Keep architecture decisions as numbered Markdown files in `docs/adr/`, in the repo,
reviewed in the same pull request as the change they describe.

## Consequences

A decision is only "made" once its ADR is merged, which costs a few minutes per
decision. In exchange, the reasoning survives the people who made it, and a
reversal is a new ADR rather than an argument. Small or obvious choices stay out
so the log keeps signal.
