# Progress (one step per iteration; tick when done, add a one-line note)

- [x] A1 cases for use cases at positions 0-20 (21 cases: 14 offscope, 6 partial, 1 live; ids verified against use_cases.json)
- [x] A2 cases for positions 21-41 (N070-N090; 42 cases total: 30 offscope, 10 partial, 2 live; ids verified)
- [x] A3 cases for positions 42-62 (N102-N125; 63 cases total: 49 offscope, 12 partial, 2 live; ids verified)
- [x] A4 cases for positions 63-83 (N126-N145, SU013; 84 cases total: 68 offscope, 14 partial, 2 live; all ids match use_cases.json in order)
- [x] A5 validate cases.json (84 cases, ids in order, schema ok, approve=false everywhere, every machine/job/item/status found in mes_facts.json; hand-written expectations N070 = 69 min and N068 = 72 min re-derived from the real job rows)
- [x] B1 smoke run (3 cases: N029 live, N027 partial, N077 offscope; all ran with the real login, real tools, no MES failures; answers read and sensible; also found+fixed a real MES concurrency bug, see Notes)
- [x] B2 full run (19:00-19:07, 84/84 cases, 0 runner errors, 0 empty answers, 0 MES-transport failures, median 13.4 s per case, max 34 s; out/results.json 169 KB; mes-eval container exited 0)
- [x] B3 grade + spot-check (84/84 graded, 0 ungraded; spot-checked 8 verdicts against the answers; v1 mixed "behaved honestly" with "delivered the use case" (26 correct refusals labelled pass), so the reviewer now also scores delivery (delivered/partly/not supported) and behaviour (correct/flawed) separately; v1 kept as out/results.graded.v1.json; final: delivery 58 not supported + 26 partly + 0 fully delivered; behaviour 56 correct + 28 flawed; result field 50 not supported / 21 partial / 12 fail / 1 pass)
- [x] B4 workbook built and copied to Downloads\MES_Evaluation_Results.xlsx (sheets Summary / Results / Gaps / Flawed answers; 102 summary formulas; 48 of them recomputed independently with the `formulas` library and matched my own counts: 48/48; report.py now scores Delivered? and Answer quality separately)
- [x] B5 final summary written for the user; loop stopped. Caveat recorded: during the run 53 DesignCenter (cc_*) calls failed because CC_DB_URL (ai_reader @ DC08, port 5433) is rejected: neither the DC08 database nor an ai_reader role exists on the running Postgres (databases there: taisin, taisinAIhub, ...). So DesignCenter lookups were impaired for this run and for the live chat; re-run B2-B4 after the DB login is fixed (about 10 minutes). Browser login test (user request) done separately after the loop.

GRADER_SUBJECT (exact text for B3):
a manufacturing assistant (cableAI chat) that can see the plant's live MES data only through read-only tools: production jobs with their lots, production orders, and lot inventory. It has no machine recipes, temperatures, historical line speeds, die or tooling data, sensor feeds or prediction models. Any claim about MES facts that no successful MES tool call supports is a fail. Saying clearly that the data or capability does not exist, and offering what it can show from the MES, is "not supported", not a fail.

Notes:
- B1 attempted: all 3 smoke cases (N029, N027, N077) failed in 0.0s with "cableCORE API reference is unavailable". Cause: the cablecore pack
  fetches /openapi.json with the user's JWT; the eval ran with EVAL_NO_LOGIN (empty token) and the BE now (correctly) refuses that with 401.
  Not a harness bug in the cases. BLOCKED on a decision: run with a real eval login (EVAL_USER / EVAL_PASS in aihub .env; faithful to the
  product) or build a MES-only agent profile (no login; simplified prompt, so not identical to production). Loop paused until decided.
  Smoke output kept in out/smoke.json (to be overwritten by the re-run).
- DECISION (user): use a real DesignCenter test login. Waiting for the user to add EVAL_USER / EVAL_PASS to the aihub .env and say so;
  then restart the loop at B1 (PLAN.md commands already updated: no EVAL_NO_LOGIN).
- USER REQUEST (19:01): give a short status update every 5 minutes while this runs, and, AFTER B2 finishes (never during: a second DCAdmin login
  replaces the session the eval run holds and would break its cableCORE tool calls), log in to DesignCenter as DCAdmin in a headless browser
  (browser-automation skill; credentials are in aihub-main/.env.eval, never echo them) and try the chat myself: an MES question (86 jobs on ARM-1
  _New may have changed), a construction question (pictures), and "show the drawing of design W212014910121720OP5". Report what rendered.
- RESOLVED: user gave the DCAdmin login; it lives in aihub-main/.env.eval (git-excluded via .git/info/exclude, NOT in .env, so the compose
  app container never sees it). Commands now pass `--env-file .env --env-file .env.eval`. The runner signs in once per run.
- FOUND+FIXED in B1 (affects the live chat too): ai/mes.py reused JSON-RPC id 1 for every request on a shared MCP session, so parallel tool calls
  got each other's replies (job A's details returned for job B). Now a unique id per request plus a check that the reply id matches (a mismatch is an
  error, never someone else's data). Verified against the real MES (6 parallel lookups x 2 rounds, 12/12 right) + regression test. 20 aihub tests pass
  in a clean environment (test_types no longer needs a real LLM key). Live aihub rebuilt with the fix.
- Smoke answers confirm the expected shape: N029 computed yield 73.9% correctly but omitted required/error qty (reviewer should say partial);
  N027 admitted no speed data but then offered generic options; N077 named no die and asked for the die list.
