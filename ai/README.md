# cableAI agent core (`ai/`)

The agent: model loop, tools, write harness, prompt and checks. No web framework, no per-user state.
`backend/` imports it and owns everything around a turn: sign-in, chat history, streaming to the FE,
waiting for card answers, files, the sandbox read gateway.

```
FE chatbot ──(cableCORE JWT)──► backend/ ──import──► ai/
                                 auth, history,       loop, tools, harness,
                                 streaming, cards      prompt, checks
                                                          │
                              cableCORE API (user's JWT) · Postgres ai_reader · sandbox (Open Terminal)
```

## Using it from the backend

```python
from ai import Answer, Card, TurnContext, TurnEvent, run_turn, user_message, make_title
from ai import http, sandbox, settings                # http.close() on shutdown; sandbox.save_file / read_file for files
from ai.apps.cablecore.index import is_read_route     # sandbox gateway check

settings.require()                                    # at startup: fails on a missing key
ctx = TurnContext(user_id, user_email, cc_token, chat_id, wait_answer=..., tickets=...)
message = user_message(text, attachment=(name, sandbox_path) or None, screen='/design/123')
async for event in run_turn(ctx, history, message):   # TurnEvent models, validated
    send(event.model_dump_json())                     # to the FE; store `Items` event's .items in the chat history
title = await make_title(text, reply)                 # first turn only
```

Contract: Pydantic models in [context.py](context.py) (`TurnEvent` is a union on `type`; `Card` a union on `kind`).
The loop itself builds plain dicts; `run_turn` validates each into its model, so nothing malformed reaches the FE.

| Backend provides | Meaning |
|---|---|
| `cc_token` | the user's cableCORE JWT, validated on every request; all cableCORE calls run as the user |
| `wait_answer(card_id, card: Card) -> Answer` | wait for the user's answer to this card (it also goes out as a `card` event, possibly after this call starts); keep the pending card so a reload can still answer it; on timeout return `Answer()`. The answer is validated strictly: unknown fields are rejected and only the boolean `True` approves |
| `tickets.issue(user_id, chat_id)` | short-lived (15 min) read ticket for sandbox scripts; the gateway checks it |
| `history` | the items of earlier turns, exactly as their `items` events gave them |

**Events**, in order: `text.delta`, `tool.start`, `tool.end`, `card`, `check`, then `items` and `done` (always), `error` on failure.
**Cards**: `approval` (one per cc_action batch, every change listed; answer `{'approved': bool}`) and
`question` (1-3 questions, 2-3 options; answer `{'status': 'answered', 'answers': {id: label or text}}`).
**Uploads**: `path = await sandbox.save_file(name, content, mime, folder='uploads')`, then `user_message(text, (name, path))`.
**Downloads**: replies link sandbox files as `/sandbox/<path>`; serve them with `sandbox.read_file(path)`.
**Sandbox gateway** (backend route, `/api/cablecore/api/{path}`, the sandbox's `CC_API_BASE`): valid ticket →
`await is_read_route(method, path, token)` → forward with the user's JWT; otherwise 403 "the sandbox has read-only access".

## Layout

```
ai/
  context.py      contract (Pydantic): TurnContext, cards, Answer, TurnEvent, WriteRecord, user_message
  settings.py     all configuration (pydantic-settings), require() for startup
  loop.py         the model/tool loop, sub-agents (delegate_task); works on plain dicts
  tools.py        function -> tool: signature -> Pydantic argument model -> JSON schema; validation; 40k cap
  http.py         shared aiohttp session, model client (LangSmith-traced when enabled)
  langchain.py    LangChain: the agent's tools as StructuredTools; chat model with Pydantic output
  ask_user.py     question card          web.py   search_web, fetch_url
  sandbox.py      sandbox tools (from Open Terminal's OpenAPI), file read/save
  titles.py       chat title
  skills.py       skills (knowledge loaded on demand), load_skill, tool search
  skills/         core skills (chat-output)
  apps/cablecore/ the cableCORE app: api, index, actions, records, related, formula, notes,
                  tools, prompt.md/prompt.py, hooks (after-turn checks), app_notes.md,
                  skills/ (searching, action-catalogue, data-model, schema, sandbox-analysis),
                  sandbox/ (image + start script), evals/ (145 cases, runner, grader, scoreboard)
```

A new app is a new folder in `apps/` exposing `instructions(ctx)`, `tools()` and `after_turn(ctx, request, reply)`;
`TurnContext.app_id` picks it.

## Environment (`cableAI/.env`)

| Var | Default | Use |
|---|---|---|
| `LUNA_BASE_URL`, `LUNA_API_KEY`, `LUNA_MODEL` | — | Azure OpenAI (Responses API) |
| `CC_API_URL` | `http://localhost:5000` | cableCORE backend (needs `OPENAPI_DOCS=true`) |
| `CC_DB_URL` | — | read-only Postgres DSN (`ai_reader`) |
| `CC_TERMINAL_URL`, `OPEN_TERMINAL_API_KEY` | `http://localhost:8070` | sandbox |
| `CC_SCHEMA_DIR` | `../cableCORE_BE/prisma/models` | Prisma schema files |
| `CC_NOTES_FILE` | `apps/cablecore/app_notes.md` | app notes |
| `CC_INDEX_TTL_S` | `1800` | route index / prompt rebuild interval |
| `AGENT_REASONING_EFFORT`, `AGENT_MAX_STEPS`, `SUBAGENTS_MAX_CONCURRENT`, `TOOL_RESULT_MAX_CHARS` | medium, 100, 6, 40000 | loop |
| `CHECK_MODEL` | the chat model | model for the after-turn checks and titles |
| `TOOL_SEARCH` | off | sandbox and web tools hidden until the model finds them with the Responses API tool search |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT` | off | LangSmith tracing of every model call. Sends chat content to LangSmith: use a self-hosted endpoint or clear it first |
| `LANGFUSE_TRACING`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` | off | Langfuse tracing (self-hosted): one trace per chat turn, session = chat, every model call nested under it. Traces contain chat content |

All are declared and validated in [settings.py](settings.py).

## Context: what each request carries

The system prompt holds only what nearly every turn needs: role, tools, rules for changes, key facts, the app
notes, and one line per skill. Everything else is disclosed when the model asks for it:

- **Skills** ([skills.py](skills.py)): a folder with `SKILL.md` (a `---` header with `name` and a `description`
  saying when it helps, then the body). The prompt lists the descriptions; `load_skill(name)` returns the body as a
  tool result. Core skills are in `ai/skills/`, an app's in `apps/<app>/skills/`. Write knowledge (formats,
  facts, examples), not steps the model must follow; keep a body under 12k characters and put more in files the
  body names (`load_skill(name, file)`). `{{PLACEHOLDERS}}` are filled by the app (`prompt.fill`).
- **Tool search** (`TOOL_SEARCH=true`): tools built with `defer=True` (sandbox, web, the sandbox ticket) are sent
  with `defer_loading`; the model loads them through the provider's tool search when it needs them.
- **Tool results** carry what the next decision needs: `cc_search` returns compact rows, cuts by whole rows
  (valid JSON), and on zero matches says how many each filter matches alone and which field names do not exist.

Measure a change with the eval runner plus `evals/scoreboard.py` (steps, tool calls, uncached/cached tokens,
time, grade per case, read from Langfuse).

## LangChain

```python
from ai.langchain import langchain_tools, structured

tools = await langchain_tools(ctx)          # StructuredTools for a LangGraph/LangChain agent: same Pydantic
                                            # arguments, same validation, cc_action still asks approval (ctx.emit)
verdict = await structured(MyModel).ainvoke(prompt)   # a validated MyModel
```

## Commands (from `cableAI/`)

```
uv sync                                                    # Python 3.12, all dependencies
uv run pytest -q                                           # tests (no services needed)
uv run ruff check ai tests
uv run pyright                                             # types: strict on the contract/core, standard elsewhere
bash ai/apps/cablecore/sandbox/start-sandbox.sh            # the sandbox container
EVAL_USER=... EVAL_PASS=... uv run --env-file .env python -m ai.apps.cablecore.evals.run out.json 3 [ids...]
```
