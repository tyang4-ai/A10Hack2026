# Backend build board

Kept by the orchestrator (backend/RUNBOOK.md section 5). Newest state at the top, history in Log.

## Now

- Stage: S0 Preflight, not started. Coding starts 8:00 PM Saturday.
- Running: nothing.
- Next: G0 checks (RUNBOOK.md section 4).

## Plans

| Plan | Agent | Stage | Status | Tests | Notes |
|---|---|---|---|---|---|
| P-scaffold | scaffolder | S1 | todo | | |
| A-setup | aws-operator | S1 | todo | | |
| B-store-api | builder | W1 | todo | | |
| B-gateway | builder | W1 | todo | | |
| B-verify | builder | W1 | todo | | |
| B-ingest | builder | W1 | todo | | |
| B-shl-plan | builder | W1 | todo | | |
| B-extract | builder | W2 | todo | | |
| B-reason | builder | W2 | todo | | |
| B-brief | builder | W2 | todo | | |
| B-cli | builder | W2 | todo | | |
| A-deploy | aws-operator | W2 | todo | | |
| B-live | builder | W3 | todo | | |
| B-baseline | builder | W3 | todo | | |
| B-p1 | builder | W3 | todo | | |

## Gates

| Gate | Result | When | Notes |
|---|---|---|---|
| G0 Preflight | | | |
| G1 Scaffold and AWS | | | |
| G2 Core | | | |
| G3 Pipeline and pre-smoke | | | |
| G4 Deploy | | | |
| G5 Full SHL run, demo-ok-1 | | | |
| G6 Demo rehearsal | | | |

## AWS

See backend/AWS_RESOURCES.md. Model ids, concurrency and the invoke URL go here once known.

## Decisions

## Known issues

## Needs Gordon

## Log
