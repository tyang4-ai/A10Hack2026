# Backend build board

Kept by the orchestrator (backend/RUNBOOK.md section 5). Newest state at the top, history in Log.

## Now

- Stage: W1 Core (started 7:30 PM). G1 passed 7:29 PM.
- Running: B-store-api, B-gateway, B-verify, B-ingest, B-shl-plan (5 builders in parallel).
- Next: G2 when all five REPORTs are in. Cutoff 11:00 PM.

## Plans

| Plan | Agent | Stage | Status | Tests | Notes |
|---|---|---|---|---|---|
| P-scaffold | scaffolder | S1 | done | 8 passed | 43 modules import, no heavy imports at load. Role agent ids 2 digits (extractor-01) |
| A-setup | aws-operator | S1 | done | models, guardrail, 50+8 parallel ok | 5.5 models denied by workshop allowlist, using Sonnet 5 + Haiku 4.5 |
| B-store-api | builder | W1 | running | | |
| B-gateway | builder | W1 | running | | |
| B-verify | builder | W1 | running | | |
| B-ingest | builder | W1 | running | | |
| B-shl-plan | builder | W1 | running | | |
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
| G1 Scaffold and AWS | pass | Sat 7:29 PM | requirements install, imports ok, 26 event types, pytest 8 passed, .env has every .env.example key, AWS_RESOURCES.md has delete commands, concurrency 32/8 |
| G2 Core | | | |
| G3 Pipeline and pre-smoke | | | |
| G4 Deploy | | | |
| G5 Full SHL run, demo-ok-1 | | | |
| G6 Demo rehearsal | | | |

## AWS

See backend/AWS_RESOURCES.md. Model ids, concurrency and the invoke URL go here once known.

- Models: large `us.anthropic.claude-sonnet-5`, small `us.anthropic.claude-haiku-4-5-20251001-v1:0`. Both pass a forced tool call.
- Concurrency: 50 parallel Haiku 50/50 ok, p95 931 ms. 8 parallel Sonnet 8/8 ok, p95 1.5 s. No throttles. `.env`: MAX_CONCURRENCY_SMALL=32, MAX_CONCURRENCY_LARGE=8.
- Prices (USD per Mtok, us.* regional): Sonnet 5 2.20 in / 11.00 out, Haiku 4.5 1.10 / 5.50.
- Guardrail `axelrod-tripwire` v1: injection sample INTERVENED, clean page NONE.
- Storage: S3 `axelrod-vault-mfafxo`, DynamoDB `axelrod-runs` and `axelrod-events`. IAM role and instance profile `axelrod-ec2` created.
- Invoke URL: not yet (A-deploy, W2).

## Decisions

- Sat 7:15 PM: start S1 right after G0 instead of at 8:00 PM (Gordon's call). Cutoffs stay at their clock times.
- Sat 7:15 PM: venv is Python 3.12 via uv (no pip inside, so installs use `uv pip install --python .venv/Scripts/python.exe`). Code targets 3.11 for EC2.
- Sat 7:16 PM: no git identity on this machine. Commits use `git -c user.name=Claude -c user.email=noreply@anthropic.com`, matching the earlier backend commits. Git config is not changed.
- Sat 7:25 PM: build on Sonnet 5 (large) and Haiku 4.5 (small), since the 5.5 models are denied. Swap is one `.env` change plus `infra/ec2_role.py` if organizers allow 5.5.

## Known issues

## Needs Gordon

- (7:25 PM, not blocking) The workshop blocks Claude Sonnet 5.5 and Haiku 5.5 (policy `ws-bedrock-model-allowlist-policy-1`). We are building on Sonnet 5 and Haiku 4.5, which work. If you want 5.5, ask an ACM organizer to allow `anthropic.claude-sonnet-5-5` and `anthropic.claude-haiku-5-5`. Either way, Devpost and the pitch should name the models we really use.

## Log

- Sat 7:15 PM: G0 pass. Guard self-test denied as expected, test_guard 80/80, AWS account is the workshop one, `origin/main` b61ffa0 already merged, venv created with base packages.
- Sat 7:25 PM: A-setup done. All S1 AWS resources created and tested. 5.5 models denied, substitutes in .env.
- Sat 7:29 PM: P-scaffold done. G1 pass. Added 6 non-AWS default keys to .env (DATA_DIR, FAKE_MODELS, HTTP_USER_AGENT, AXELROD_API_KEY, PUBLIC_UI_URL, DEMO_PACING).
- Sat 7:30 PM: W1 launched (5 builders).
