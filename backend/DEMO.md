# Live demo: the 3-minute webapp run

This is the live part of the 3-minute demo (docs/PLAN.md, 0:15 to 1:20). We demo in person and click it ourselves. Since 2:57 AM the server runs `STAGE_PACING=3` (a pause at the start of each stage) instead of the old per-fetch `DEMO_PACING`, which is gone from the code. The timings below were measured live by the integrator on run_20261108_0ntqmj at 3:10 AM. The live part costs about $0.10 to $0.20 up to STOP.

Say these on stage: the plan is frozen (prepared before the event), the archive documents were collected before the event and are served from our S3 cache, and S-010 (the injection page), S-017 (the broken download) and the one malformed judge verdict are planted and simulated.

## Before the demo

Server env on EC2 (`bash backend/infra/remote.sh --set-env KEY=VALUE ...` sets `/etc/axelrod.env` and restarts only the `axelrod` service):

```
CACHE_DOCUMENTS=on            # fetches are decided by the gateway, then served from the S3 cache
CACHE_MODELS=replay           # agent model calls are answered from two recorded runs at $0 when they match; a call that doesn't match goes live (slower), so a run never fails. Agent chat is always live. Runs say documents_and_models: say the model answers are recorded
STAGE_PACING=1                # seconds of pause at the start of each stage (never at approve). STOP ends it at once
FETCH_PACING=1.45             # seconds before each fetch decision (a revoke in that time still blocks it)
VERIFY_PACING=3               # seconds per judge call, so verification visibly takes time
DEMO_FAULTS=fetch,judge       # planted faults, labeled simulated: S-017 broken download, one malformed judge verdict
PROTECTED_RUNS=run_20261108_rzpoj7,run_20261108_pin2ve    # DELETE refuses the demo run and its backup
MAX_CONCURRENCY_SMALL=48      # model slots (48 small, MAX_CONCURRENCY_LARGE=8)
BUDGET_USD=50                 # cap per run (a full run costs about $3)
GUARDRAIL_ID=...              # already set: the tripwire must be live
TEST_PAGE_URL=<invoke-url>/test-pages/injection.html
```

Since 6:03 AM two full-run click paths are recorded and replay at about $0 in under 2:00 from launch, with Watch (extract plus draft) under 30 s:
- **Approve as planned:** click Approve on the priority sources, change nothing. 117 s from launch, Watch 17 s (run_20261108_cyfrac).
- **Stage clicks:** drag S-002 above S-001, reject S-016, Approve, then revoke S-006 right away. 109 s from launch (run_20261108_9wvm74).

**No goon stop in a full run** (the user's call): stopping a goon changes every later prompt, so those calls go live and Watch runs past 2 minutes. Show stop-one-agent on a separate run and press STOP right after it. Other clicks still finish, just slower. `FETCH_PACING` and `VERIFY_PACING` are the knobs. **Talk to an agent at any time, fetching included:** Bobby answers live in about 4 s (chat always skips the recorded answers and never changes the run, so the replay and its timing stay the same). The fetcher has no model, so a message to it fails: the gateway blocks it ("Role 'fetcher' may not call models"). Talk to Bobby during fetching, and to an extractor, drafter or judge once Watch or Review shows them. The click-by-click timings below were measured at STAGE_PACING=3 with live models, so expect fetching to be a little slower and everything after it faster.

## On stage

Times are seconds after clicking Approve (run_20261108_0ntqmj).

| When | Do | What appears |
|---|---|---|
| Launch | In the terminal: `python -m backend.axelrod.cli run "Siemens Healthineers" --ticker SHL`. Switch to the webapp | The run is there. Bobby is the first agent. The plan streams into Sources (frozen, say so), then Bobby's plan-gate call shows in the log: proceed. A typo in the name still finds the plan, and the run notes say so |
| Approval | Approve every priority source, drag S-002 above S-001, reject S-016 (Philips 2023: one page, adds no data). Never touch S-006, S-009, S-010 or S-017. Click Approve | Approval committed. S-016 gets a reject rule |
| 0 to 3 s | **Right after Approve, click Revoke on S-006** in Sources, during the 3 s pause before collect | Policy updates with R-revoke-S-006 |
| 3 to 19 s | Nothing | Traffic fills in, S-002 first. Each fetch says cache hit and when the copy was collected (2026-08-01 for the archive: say it was collected before the event) |
| about 19 s | Nothing | S-006's request turns red: R-revoke-S-006 |
| about 20 s | Nothing | S-009's off-site links turn red on their own (default deny, no rule allows them). S-010, our planted test page (simulated), comes back flagged by Bedrock Guardrails (PROMPT_ATTACK) and is quarantined |
| about 21 s | Nothing | S-017, our planted broken download (simulated), answers HTTP 503 twice: the fetcher retries once, then marks it failed. It becomes a declared hole, not a silent gap |
| about 22 to 27 s | In the Agents window, stop one extractor that still shows queued | Extract starts after the 3 s pause, and the first extractor calls go green at about 27 s. The stopped goon's next model call turns red (R-stop-<agent id>) while the other goons stay green |
| a few seconds later | Press STOP | In-flight calls come back blocked ("Result discarded") and nothing is allowed afterwards. The run shows stopped |

The malformed judge verdict (simulated) shows only in a run that reaches verify: one judge's answer has no verdict, code flags it in the log and reruns that judge. The live part stops in extract, so it isn't seen there. R2, the finished run, was made before the planted faults existed.

CLI fallback for each click: `cli approve <run_id> --all`, `cli revoke <run_id> S-006 --reason "Analyst revoked mid-run"`, `cli stop <run_id> --reason "Presenter pressed STOP"`.

## After

Open the finished SHL run for 1:20 to 2:35. Its id is the first line of `backend/status.md` ("DEMO RUN: ..."). In the source viewer, Page shows the real annual report page with the quote boxed (green pass, amber fixed, red fail). Text shows exactly what the checker matched.

Test runs can go with "Delete other runs" on the home page. The demo run and its backup show "Demo" and the server refuses them.

## Fallbacks

- **Bedrock is slow or throttles:** `bash backend/infra/remote.sh --set-env CACHE_MODELS=replay`. Recorded model answers (R2 and every run since, while `record` was on) are served at $0 (`cache_mode: documents_and_models`). Bobby's gate calls and the judge rerun miss and go live. Say the model answers are recorded. Set it back to `record` afterwards.
- **The live run breaks:** open the finished run and walk the four steps through its recorded traffic. Say it's recorded.
- **AWS is down:** on the laptop, the API serves the exported runs from `backend/data/replay/` as `mode: replay` (`STORE=local`). Say it's a replay.
