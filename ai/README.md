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
| `memory` (optional) | `MemoryStore` for the account: `recall`, `remember`, `search` (earlier chats and their files); gives the model `remember` and `search_chats` |

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
| `CC_DB_URL` | — | read-only Postgres DSN for the `ai_reader` role. Create or refresh the role with `scripts/create_ai_reader.py` (SELECT only; no credential, secret, chat or audit tables; 30 s statement timeout; `--verify` checks it; re-run it after a migration adds tables, new tables are not readable until then). In Docker use `host.docker.internal`: the host is kept inside a container and rewritten to `localhost` when the process runs on the host |
| `CC_PUBLIC_URL` | `http://localhost:5000` | the backend as the user's browser reaches it: design drawings in the chat load from `<CC_PUBLIC_URL>/erp/<design id>/drawingurl` (the browser sends its own session cookie; the frontend and the API must share a site) |
| `SERPAPI_KEY` | — | picture search for the chat (`search_images`, SerpAPI Google Images, SafeSearch on, only Google thumbnails are returned; 10 searches per user per hour, answers cached for an hour). Empty = no such tool |
| `MES_MCP_URL`, `MES_USER_TOKENS` | —, — | MES (Mendix MCP server) tools, see below. `MES_USER_TOKENS` is JSON `{"cableCORE username": "token"}`. Empty URL or no token for the user = no MES tools |
| `CC_TERMINAL_URL`, `OPEN_TERMINAL_API_KEY` | `http://localhost:8070` | sandbox |
| `CC_SCHEMA_DIR` | `../cableCORE_BE/prisma/models` | Prisma schema files |
| `CC_NOTES_FILE` | `apps/cablecore/app_notes.md` | app notes |
| `CC_INDEX_TTL_S` | `1800` | route index / prompt rebuild interval |
| `AGENT_REASONING_EFFORT`, `AGENT_MAX_STEPS`, `SUBAGENTS_MAX_CONCURRENT`, `TOOL_RESULT_MAX_CHARS` | medium, 100, 6, 40000 | loop |
| `CHECK_MODEL` | the chat model | model for the after-turn checks and titles |
| `AGENT_COMPACT_TOKENS` | `150000` | past this many input tokens in a step, all but the newest 6 tool results are cut to a stub; a context-length error compacts harder and retries once |
| `MINIO_ENDPOINT`, `MINIO_PORT`, `MINIO_USE_SSL`, `MINIO_USER_NAME`, `MINIO_PASSWORD`, `MINIO_BUCKET` | —, 9000, false, —, —, `ai-chat-files` | backend: chat uploads in cableCORE's MinIO (same values as cableCORE_BE's app config); empty endpoint = text only |
| `TOOL_SEARCH` | off | sandbox and web tools hidden until the model finds them with the Responses API tool search |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT` | off | LangSmith tracing of every model call. Sends chat content to LangSmith: use a self-hosted endpoint or clear it first |
| `LANGFUSE_TRACING`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` | off | Langfuse tracing (self-hosted): one trace per chat turn, session = chat, every model call nested under it. Traces contain chat content |

All are declared and validated in [settings.py](settings.py).

## MES tools and the MES evaluation

`ai/mes.py` hands the model the user's plant MES as `mes_*` tools: live, read-only, one token per user. The tool list comes from the server itself (cached 10 minutes), the MES's `{"tool", "data"}` wrapper is removed before the model sees an answer, and every request carries its own JSON-RPC id (the tool calls of a step run in parallel over one session; an answer to a different request is refused, never passed on). The MES instructions in `mes.py` (`PROMPT`) are part of the behaviour: plant facts only from tool results, never an unsupported number, say what data is missing, look before saying "not found".

The evaluation (`apps/cablecore/evals/`) runs cases through the real loop (`run.py`) and has an LLM reviewer score each one (`grade.py`): **delivered?** (delivered, partly, not supported: a correct refusal is "not supported / correct") and **answer quality** (correct, flawed). The 84 MES use cases are in `evals/mes/` (`PLAN.md` has the exact commands, `report.py` builds the Excel report, `ui/` has browser scripts for an end-to-end check in the UI).

- Credentials stay out of the image and the repo: the evaluation signs in with `EVAL_USER` / `EVAL_PASS` from `.env.eval` (git-excluded; a run signs in once and ends that user's browser session). `EVAL_USER_EMAIL` picks the user whose MES token is used. Run it with `SERPAPI_KEY=` empty so it cannot spend picture-search quota.
- The reviewer sees tool names and arguments, not tool outputs: a claim it cannot verify may be flagged as unsupported. Read a few verdicts before trusting the totals.
- Results of earlier runs are kept in `evals/mes/out/run*/`; compare like with like (the MES tool set and the code change between runs).

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

## Per-turn context, access, files, memory

- **Access** (`apps/cablecore/access.py`): `GET /auth/access` (cached 60 s) gives the user's active plant, plants,
  and every licence key and restriction as the backend's AccessInterceptor evaluates them; the OpenAPI `x-access`
  of each route says what it needs. `cc_find`/`cc_describe` mark routes the user may not call and `cc_action`
  refuses them before validation or any approval card; `cc_get`/`cc_related`/`cc_formula` gate tables
  (credentials and chats never; cost, formulas, enquiries, tenders by licence). The API still enforces.
- **Active plant**: `cc_search`, `cc_related` and `cc_formula` default to the active plant; a named plant or
  `"all"` overrides; results say which plants were covered.
- **Per-turn block**: the app's `turn_context(ctx)` (who, plant, what is not licensed / read-only / not
  permitted) and the user's memories are appended to the user message, never to the cached system prompt.
- **Files**: uploads go to MinIO and `chat_file` (extracted text, `ai/documents.py`); up to 120k characters the
  whole document is in the user message, so later turns answer from history without re-reading.
- **Long chats**: messages older than the replayed window (60) are kept as `chat_session.summary`, refreshed in
  the background every 10 messages (`backend/app/chat/summary.py`).

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
