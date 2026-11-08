# Live demo: A10's four steps

The live part of the 4-minute demo (docs/PLAN.md, 0:20 to 1:40). The commands below come from B-live's rehearsal, which passed every beat twice against live Bedrock and the real guardrail on 2026-11-07. The test page is ours and simulated: say so on stage.

## Before the demo

Server environment on the demo instance (`/etc/axelrod.env` on EC2, then restart the `axelrod` service):

```
DEMO_PACING=2                 # seconds before each fetch, so there is time to revoke S-006
MAX_CONCURRENCY_SMALL=8       # fewer metered in-flight calls get discarded at STOP (32 is for the full run)
GUARDRAIL_ID=...              # already set, the tripwire must be live
TEST_PAGE_URL=<invoke-url>/test-pages/injection.html
```

Presenter terminal:

```
export AXELROD_API=<invoke-url>/api/v1      # PowerShell: $env:AXELROD_API = "<invoke-url>/api/v1"
```

Rehearse once against the same API (it creates its own run and stops it, under $0.10):

```
python backend/scripts/demo_live.py --api $AXELROD_API --pacing-check
```

## On stage

Two terminals side by side, plus the dashboard.

1. **Launch.** The run shows up in the UI and the plan streams in.
   ```
   python -m backend.axelrod.cli run "Siemens Healthineers" --ticker SHL
   python -m backend.axelrod.cli tail <run_id>        # second terminal
   ```
   Approve in the UI: priority sources, drag one up, reject one. Never reject or move up S-006, S-009 or S-010. Fallback without the UI:
   ```
   python -m backend.axelrod.cli approve <run_id> --all
   ```
2. **Traffic.** Nothing to type. The tail shows every fetch (HTTP status, bytes) and every model call (tokens, cost, running total). Cyan `NEXT` lines say which source comes next.
3. **Block.**
   - S-009's off-site links go red on their own: default deny.
   - Revoke S-006 as soon as the first fetch lines appear (the annual report, S-001). Have the command typed in advance. S-006 is fetched 8th, about 20 s into collect at `DEMO_PACING=2`, so there is plenty of time. Don't wait for `NEXT S-006`: that line comes only 2 s before the request. When the fetcher reaches it, the request turns red with `R-revoke-S-006`:
     ```
     python -m backend.axelrod.cli revoke <run_id> S-006 --reason "Analyst revoked mid-run"
     ```
   - S-010, our planted test page (simulated), comes back yellow, FLAGGED by Bedrock Guardrails (PROMPT_ATTACK), and its document is quarantined. No agent ever reads it, and its example.org link is blocked.
4. **Stop.** As soon as the tail shows the first extract model call:
   ```
   python -m backend.axelrod.cli stop <run_id> --reason "Presenter pressed STOP"
   python -m backend.axelrod.cli status <run_id>
   ```
   In-flight model calls come back blocked ("Result discarded"), and nothing is allowed afterwards.

## After

Score the stage run from its events (sends nothing):

```
python backend/scripts/demo_live.py --api $AXELROD_API --check <run_id>
```

Then switch to the finished SHL run (G5) for 1:40 to 3:00. If AWS is down, the API serves the exported run from `backend/data/replay/` as `mode: replay`. Say it's a replay.
