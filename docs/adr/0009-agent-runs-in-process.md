# 0009. The agent runs in process; turns pause on cards

Status: Proposed
Date: 2026-11-13

## Context

The backend called the AI as a separate HTTP service (`AI_BASE_URL/chat`, one blocking request, the
last 20 messages as text). The agent in `ai/` is a Python package: it streams events, and in the
middle of a turn it can wait for the user, to approve the exact changes it is about to make in
cableCORE or to answer a question. It also needs its own record of earlier turns (tool calls,
results, encrypted reasoning); text alone loses what it looked up and changed.

The frontend already speaks cableCORE's chat contract: `/chat/send` (blocking) and `/chat/stream`
(server-sent `session`, `steps`, `delta`, `note`, `done`, `error`), with approval questions in the
reply and the user's choice sent as the next message.

## Decision

The backend imports `ai` and runs each turn in process (`app/chat/agent.py`), translating the agent's
events into that contract. When the agent asks for a card, the request ends with it as `questions`
and the agent turn stays paused; the next message in the session is its answer and resumes it. The
agent's record of each turn is stored on the turn's final AI message (`chat_message.agent_items`,
migration 0002) and replayed as history: the newest 3 turns in full, older ones as conversation only.

## Consequences

No second service, no network hop, and the frontend keeps its contract. A paused or running turn lives
in the backend process: one worker per chat (or sticky sessions) until the registry moves to Valkey.
An unanswered card declines after 10 minutes. Leaving mid-stream stops the turn. The agent's settings
(`LUNA_*`, `CC_*`) join the backend's environment. `AI_STUB` still answers without the agent.
