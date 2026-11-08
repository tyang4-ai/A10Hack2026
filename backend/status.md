# Backend build board

Kept by the orchestrator (backend/RUNBOOK.md section 5). Newest state at the top, history in Log.

## Now

- Stage: W1 Core (started 7:30 PM). G1 passed 7:29 PM.
- Running: B-verify. Done: B-store-api, B-gateway, B-ingest, B-shl-plan.
- Next: G2 when all five REPORTs are in. Cutoff 11:00 PM.

## Plans

| Plan | Agent | Stage | Status | Tests | Notes |
|---|---|---|---|---|---|
| P-scaffold | scaffolder | S1 | done | 8 passed | 43 modules import, no heavy imports at load. Role agent ids 2 digits (extractor-01) |
| A-setup | aws-operator | S1 | done | models, guardrail, 50+8 parallel ok | 5.5 models denied by workshop allowlist, using Sonnet 5 + Haiku 4.5 |
| B-store-api | builder | W1 | done | 51 passed, live S3/DynamoDB ok | contract_smoke passes vs real API and mock, restart keeps runs, 200 concurrent emits gapless |
| B-gateway | builder | W1 | done | 46 passed, 3 live passed | Haiku and Sonnet forced tool via AgentRunner live ok, guardrail flags injection. Fail closed, budget held at worst case |
| B-verify | builder | W1 | running | | |
| B-ingest | builder | W1 | done | 24 passed | ingest_one S-001 live ok (230 pages). Untrusted = news/forum/other or tier_3, scanned, fail closed. S-010 404 until pushed |
| B-shl-plan | builder | W1 | done | 31 passed, 1 skipped | 10 sources, 7 needs, 36 extract pages checked against PDFs. S-006 is the tier_2 revoke target, fetched 8th |
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
- Sat 7:33 PM: Gordon's call: from now on commits are authored only as `tyang4-ai` (repo-local git config, GitHub no-reply email), with no Claude co-author line. S0 and G1 stay as pushed (rewriting them would need a force push).
- Sat 7:25 PM: build on Sonnet 5 (large) and Haiku 4.5 (small), since the 5.5 models are denied. Swap is one `.env` change plus `infra/ec2_role.py` if organizers allow 5.5.

- Sat 7:31 PM: Gordon is away for a while. Bash is allowed without prompts in `.claude/settings.local.json` (personal, not committed). The guard hook and the deny rules still apply.

- Sat 7:53 PM: accepted B-shl-plan's choices. `check_plan.py` uses urllib (a dev check, not runtime), and S-002 is the release PDF.

## Known issues

- Guard quirk: `git push origin backend 2>&1 | tail -1` is read as a push to "2>" and asks Gordon. Orchestrator pushes are plain `git push origin backend` from now on. The guard itself is unchanged (Gordon's file).

## Needs Gordon

- (7:55 PM, needed before W2, about 9:30 PM) **The GitHub repo is private** (CLAUDE.md says public). Two things break while it stays private: (1) the raw URL of the planted test page S-010 returns 404, and (2) EC2 can't clone the repo for the deploy. Option A: make tyang4-ai/A10Hack2026 public now (Devpost needs it public anyway). Option B: keep it private until submission, and I'll deploy by copying the code to our S3 bucket (no GitHub needed) and serve the test page from our own API, labeled simulated. If I hear nothing by the time W2 starts, I'll go with B, since it works either way.

- (7:49 PM, not blocking) The reviser role gets the small model in PLANS.md but Sonnet in `mock/build_fixtures.py` ROLE_SCOPES. The real gateway follows PLANS.md. Either change the mock on main or say "reviser uses Sonnet" and I'll switch the backend. The reviser is P1 (W3), so this can wait.

- (7:31 PM, needed in W2, about 10:45 PM to 12:45 AM) A-deploy opens port 8000 on the EC2 security group. The guard asks you before that. If nobody answers in about 10 minutes, A-deploy builds everything except the open port and we use the G4 fallback (API on the laptop) until you're back.

- (7:25 PM, not blocking) The workshop blocks Claude Sonnet 5.5 and Haiku 5.5 (policy `ws-bedrock-model-allowlist-policy-1`). We are building on Sonnet 5 and Haiku 4.5, which work. If you want 5.5, ask an ACM organizer to allow `anthropic.claude-sonnet-5-5` and `anthropic.claude-haiku-5-5`. Either way, Devpost and the pitch should name the models we really use.

## Log

- Sat 7:15 PM: G0 pass. Guard self-test denied as expected, test_guard 80/80, AWS account is the workshop one, `origin/main` b61ffa0 already merged, venv created with base packages.
- Sat 7:25 PM: A-setup done. All S1 AWS resources created and tested. 5.5 models denied, substitutes in .env.
- Sat 7:29 PM: P-scaffold done. G1 pass. Added 6 non-AWS default keys to .env (DATA_DIR, FAKE_MODELS, HTTP_USER_AGENT, AXELROD_API_KEY, PUBLIC_UI_URL, DEMO_PACING).
- Sat 7:30 PM: W1 launched (5 builders).
- Sat 7:53 PM: B-ingest, B-gateway, B-shl-plan done. Pushed plan.json and the planted test page so its raw URL answers.
