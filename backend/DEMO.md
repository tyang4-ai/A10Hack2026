# Live demo: the 3-minute webapp run

This is the live part of the 3-minute demo (docs/PLAN.md, 0:15 to 1:20). The timings come from the scripted G6b rehearsals on EC2, Sun 2026-11-08 1:10 to 1:20 AM. Three runs passed every beat (`demo_live.py --webapp`, live Bedrock, the real guardrail, cached documents): run_20261108_h5suf7, run_20261108_vmhieu and run_20261108_5hddrr. A fourth, run_20261108_8c409b, passed everything but the time limit at the old 2 s pacing. Each live segment costs about $0.18.

Say these on stage: the plan is frozen (prepared before the event), the archive documents were collected before the event and are served from our S3 cache, and S-010 is our planted test page (simulated).

## Before the demo

Server env on EC2 (`bash backend/infra/remote.sh --set-env KEY=VALUE ...` sets `/etc/axelrod.env` and restarts the service):

```
CACHE_DOCUMENTS=on            # fetches are decided by the gateway, then served from the S3 cache
CACHE_MODELS=off              # model calls stay live (replay is the Bedrock fallback, see below)
DEMO_PACING=0.8               # seconds before each fetch. 0.8 lands STOP at about 52 s with a 10 s approval
MAX_CONCURRENCY_SMALL=8       # fewer in-flight calls to discard at STOP (32 is for full runs)
BUDGET_USD=1                  # cap per run. The live segment spends about $0.18
GUARDRAIL_ID=...              # already set: the tripwire must be live
TEST_PAGE_URL=<invoke-url>/test-pages/injection.html
```

We demo in person and click it ourselves (Gordon, 1 AM). Scripted rehearsals are cut. `DEMO_PACING` stays an env setting: raise it if the clicks need more time, and lower it if STOP comes late.

## On stage

Times are seconds from the launch command (rehearsal run_20261108_5hddrr, 10 s approval). With a 5 s approval everything comes about 5 s earlier.

| Time | Do | What appears |
|---|---|---|
| 0 s | In the terminal: `python -m backend.axelrod.cli run "Siemens Healthineers" --ticker SHL`. Switch to the webapp | The run is there. Bobby is the first agent |
| 4 s | Nothing | The plan streams into Sources (frozen, say so). At 5 s Bobby's plan-gate call shows in the log: proceed |
| 5 to 16 s | Approve every priority source, drag S-002 above S-001, reject S-016 (Philips 2023: one page, adds no data). Never touch S-006, S-009 or S-010. Click Approve | Approval committed. S-016 gets a reject rule |
| 17 s | **Right away, click Revoke on S-006** in Sources. Don't wait for "next: S-006": at 0.8 s pacing it comes less than a second before the request | Traffic fills in, S-002 first. Each fetch says cache hit and when the copy was collected (2026-08-01 for the archive: say it was collected before the event) |
| 39 s | Nothing | S-006's request turns red: R-revoke-S-006 |
| 42 s | Nothing | S-009's off-site links turn red on their own: default deny, no rule allows them |
| 45 s | Nothing | S-010, our planted test page (simulated), comes back flagged by Bedrock Guardrails (PROMPT_ATTACK) and is quarantined. No agent ever reads it |
| 47 s | In the Agents window, stop one extractor that still shows queued, near the top of the list | Extract starts only after collect, and the first extractor calls go green. The stopped goon's next model call turns red (R-stop-<agent id>) while the other goons stay green |
| 50 s | Press STOP | In-flight calls come back blocked ("Result discarded") and nothing is allowed afterwards. The run shows stopped |

STOP landed at 42.5 to 51.7 s in the rehearsals, inside the 55 s budget.

CLI fallback for each click: `cli approve <run_id> --all`, `cli revoke <run_id> S-006 --reason "Analyst revoked mid-run"`, `cli stop <run_id> --reason "Presenter pressed STOP"`.

## After

Open the finished SHL run for 1:20 to 2:35. Its id is the first line of `backend/status.md` ("DEMO RUN: ...").

## Fallbacks

- **Bedrock is slow or throttles:** `bash backend/infra/remote.sh --set-env CACHE_MODELS=replay`. Model answers recorded in R2 are served at $0 (`cache_mode: documents_and_models`). Bobby's gate calls miss and go live. Say the model answers are recorded. Set it back to `off` afterwards.
- **The live run breaks:** open the finished run and walk the four steps through its recorded traffic. Say it's recorded.
- **AWS is down:** on the laptop, the API serves the exported runs from `backend/data/replay/` as `mode: replay` (`STORE=local`). Say it's a replay.
