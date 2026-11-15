# MES use-case evaluation (end to end)

Scope: the **MES** sheet only (84 use cases) of `Downloads\cableAI_Use_Cases_DC_MES_Split_1 1_3308.xlsx`. Not DC, not For Review.
Question answered: for each MES use case, what does the real cableAI chat agent (live MES tools, LUNA model) actually deliver today?

## What exists (reused, not rewritten)
- `../run.py` runs each case through the real agent loop and records answer, tools, cards, latency.
  MES runs set `EVAL_NO_LOGIN=1` and `EVAL_USER_EMAIL=DCAdmin` (the user mapped to the MES token in `.env`).
- `../grade.py` is an LLM reviewer: pass / partial / fail / not supported, per use case, with a one-line reason.
- `use_cases.json` (84 rows from the workbook), `mes_facts.json` (real machines, statuses, job codes, tool names), `report.py`.

## The live MES tools (all read-only; this is all the agent can see of the plant)
`mes_get_job_list(Status, JobCode, MachineName)` · `mes_get_job_detail(JobCode)` · `mes_list_production_orders(Status)` · `mes_get_lot_inventory(ItemId, LocationName)`.
A job row has: machine, item, status, qty required/completed/good/error, planned start/end, lots. It has NO recipes, temperatures, line speeds history,
dies/tooling, sensors or predictive models. Most of the 84 use cases ask for those: expect many "not supported". That is a valid, useful result.

## Case file: `cases.json` (one entry per use case, `id` = the use-case id, e.g. "N070")
```json
{"id": "N070", "group": "Production Speed & Runtime", "turns": ["<realistic user message>"], "page": "/design", "approve": false,
 "tier": "live | partial | offscope", "check": "what a good answer must do or avoid"}
```
Rules for writing them:
1. The prompt is what a planner / supervisor / operator would really type, in their words, not the use-case title. Use the use case's
   "example" and "expected" fields in `use_cases.json` for intent. One turn, no file.
2. Ground it in REAL entities from `mes_facts.json` (machines such as ARM-1, Ext-S-4; statuses _New, Ready, InProgress, Suspended_Jobs;
   job codes such as POT5244_1; items such as ARMCUXNWT-2). Never invent a machine, job or item name.
3. `tier` (a hypothesis, not shown to the reviewer): `live` = answerable from the 4 tools; `partial` = needs the tools plus reasoning/calculation
   on what they return (runtimes, planned vs actual, utilisation, bottleneck by machine); `offscope` = needs data or models the agent does not have.
4. `check` is the answer key in plain words. For `live`/`partial`: say which tool call(s) must happen and what must be true of the result.
   For `offscope`: "Must not invent values; should say it has no <recipe/temperature/die/...> data and offer what it can show from the MES
   (jobs, orders, lots)". A confident invented number is a fail.
5. No writes: the MES tools are read-only; do not ask the agent to change anything.

## Steps (tick in PROGRESS.md; do ONE step per iteration)
A1-A4  Author `cases.json` in four batches by position in `use_cases.json` (0-20, 21-41, 42-62, 63-83): append to the array, keep ids unique.
A5     Validate: 84 cases, ids match `use_cases.json`, every case has prompt + tier + check, no invented entity names (grep against `mes_facts.json`).
B1     Smoke run on 3 cases (one per tier). Read the answers. If the harness or a prompt is wrong, fix it before the full run.
B2     Full run in the background (84 cases, concurrency 3), output `out/results.json`.
B3     Grade -> `out/results.graded.json`. Spot-check 6 verdicts by reading the answer yourself; if the reviewer is clearly wrong, note it.
B4     Build the workbook with `report.py` into `out/MES_Evaluation_Results.xlsx`, copy to `Downloads\MES_Evaluation_Results.xlsx`.
B5     Final summary for the user (counts by verdict, best 5 and worst areas, what MES data/tools would unlock the most use cases). Stop the loop.

## Commands (PowerShell; Docker Desktop; aihub image already built)
`$R='C:\WorkSpace\DesignCenter\Repository\aihub-main\aihub-main'; $env:Path="C:\Program Files\Docker\Docker\resources\bin;"+$env:Path`
Common: `docker run --rm --env-file "$R\.env" --add-host host.docker.internal:host-gateway -e EVAL_USER_EMAIL=DCAdmin -e SERPAPI_KEY=
 -e CC_API_URL=http://host.docker.internal:5000 -e CC_TERMINAL_URL=http://host.docker.internal:8070 -e EVAL_CASES=/ev/cases.json
 -v "$R\ai:/app/ai:ro" -v "$R\ai\apps\cablecore\evals\mes:/ev" --entrypoint sh aihub-main-aihub -c "cd /app && <command>"`
DECIDED: the evaluation signs in with a dedicated DesignCenter test account (EVAL_USER / EVAL_PASS in the aihub .env, put there by the user;
never ask for or echo the password). Do NOT set EVAL_NO_LOGIN (the real agent needs a session: /openapi.json requires a login).
`EVAL_USER_EMAIL=DCAdmin` stays: it only selects which MES token the MES tools use. Login happens once per run and is never retried
(3 failures lock the account): if it fails, stop and tell the user.
- run:   `uv run --no-sync python -m ai.apps.cablecore.evals.run /ev/out/results.json 3 [case ids...]`
- grade: `uv run --no-sync python -m ai.apps.cablecore.evals.grade /ev/use_cases.json /ev/cases.json /ev/out/results.json`
         with `-e GRADER_SUBJECT="..."` (see PROGRESS.md for the exact text).
- report: `uv run --no-project --with openpyxl python /ev/report.py /ev/use_cases.json /ev/cases.json /ev/out/results.json /ev/out/results.graded.json /ev/out/MES_Evaluation_Results.xlsx`
`SERPAPI_KEY=` is blanked so the evaluation cannot spend picture-search quota. Results of a smoke run must be moved aside before the full run.
