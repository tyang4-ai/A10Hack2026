# Backend build board

Kept by the orchestrator (backend/RUNBOOK.md section 5). Newest state at the top, history in Log.

## Now

- Stage: S1 Scaffold and AWS (started 7:16 PM, early by Gordon's call).
- Running: P-scaffold (scaffolder), A-setup (aws-operator).
- Next: G1 checks once both REPORTs are in.

## Plans

| Plan | Agent | Stage | Status | Tests | Notes |
|---|---|---|---|---|---|
| P-scaffold | scaffolder | S1 | running | | |
| A-setup | aws-operator | S1 | running | | |
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
| G0 Preflight | pass | Sat 7:15 PM | guard live, 80/80 guard cases, account ok, venv py3.12 (uv), main already merged |
| G1 Scaffold and AWS | | | |
| G2 Core | | | |
| G3 Pipeline and pre-smoke | | | |
| G4 Deploy | | | |
| G5 Full SHL run, demo-ok-1 | | | |
| G6 Demo rehearsal | | | |

## AWS

See backend/AWS_RESOURCES.md. Model ids, concurrency and the invoke URL go here once known.

## Decisions

- Sat 7:15 PM: start S1 right after G0 instead of at 8:00 PM (Gordon's call). Cutoffs stay at their clock times.
- Sat 7:15 PM: venv is Python 3.12 via uv (no pip inside, so installs use `uv pip install --python .venv/Scripts/python.exe`). Code targets 3.11 for EC2.
- Sat 7:16 PM: no git identity on this machine. Commits use `git -c user.name=Claude -c user.email=noreply@anthropic.com`, matching the earlier backend commits. Git config is not changed.

## Known issues

## Needs Gordon

## Log

- Sat 7:15 PM: G0 pass. Guard self-test denied as expected, test_guard 80/80, AWS account 506556958358, `origin/main` b61ffa0 already merged, venv created with base packages.
