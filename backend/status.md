# Backend build board

Kept by the orchestrator (backend/RUNBOOK.md section 5). Newest state at the top, history in Log.

## Now

- Stage: W2 Pipeline (started 8:00 PM, early). W1 is 4/5 done and committed (af7946f).
- Running: A-deploy (W2, waiting on the port approval), verifier (G3). Verifier (G3). W1 is 5/5 done.
- Next: G5 now, the full SHL run on EC2 through the CLI. Finish G3 when the verifier reports.
- **Rehearsal R0b done (9:46 PM): run_20261108_whhj0d, full SHL run, laptop, live models.** Report final: 403 items (341 pass, 40 fixed, 22 fail = 94.5% pass or fixed), 25 holes, report certainty 92. Scores: rotic_level -1 (9.4%, low), rotic_vs_peers 0 (low), secular_growth +1, leverage 0, liquidity N/A (its coverage computation failed), research_stage N/A (by design). 4 of 6 scored, weighted average 0.0. Brief final: 33 sentences (31 pass, 2 fixed, 1 removed), 2.8 pages, certainty 85. $2.06, about 5 min. contract_smoke 21/21. Exported as the replay fallback (`backend/data/replay/run_20261108_whhj0d`, 31 MB, text only).
- Rehearsal R0 (laptop, live models): run_20261108_0bsu0o reached the report in about 5 min for $1.20, then failed at summarize (the model returned a list as a string). All the live gateway beats worked. 353 data items, 86% pass or fixed, only 2 of 6 criteria scored. Three fix builders running.

## Plans

| Plan | Agent | Stage | Status | Tests | Notes |
|---|---|---|---|---|---|
| P-scaffold | scaffolder | S1 | done | 8 passed | 43 modules import, no heavy imports at load. Role agent ids 2 digits (extractor-01) |
| A-setup | aws-operator | S1 | done | models, guardrail, 50+8 parallel ok | 5.5 models denied by workshop allowlist, using Sonnet 5 + Haiku 4.5 |
| B-store-api | builder | W1 | done | 51 passed, live S3/DynamoDB ok | contract_smoke passes vs real API and mock, restart keeps runs, 200 concurrent emits gapless |
| B-gateway | builder | W1 | done | 46 passed, 3 live passed | Haiku and Sonnet forced tool via AgentRunner live ok, guardrail flags injection. Fail closed, budget held at worst case |
| B-verify | builder | W1 | done | 67 passed | planted 12/12 caught, clean 12/12 pass. Revoked source makes dependents blocked (E to C to D) |
| B-ingest | builder | W1 | done | 24 passed | ingest_one S-001 live ok (230 pages). Untrusted = news/forum/other or tier_3, scanned, fail closed. S-010 404 until pushed |
| B-shl-plan | builder | W1 | done | 31 passed, 1 skipped | 10 sources, 7 needs, 36 extract pages checked against PDFs. S-006 is the tier_2 revoke target, fetched 8th |
| B-extract | builder | W2 | done | 24 passed, live presmoke | 5 real S-001 pages: 49/49 items pass, $0.06. Total live spend so far about $0.13 |
| B-reason | builder | W2 | done | 20 passed (suite 366) | unsupported conclusion 0/3 votes and downgraded, rollup matches hand example, research_stage N/A with a Hole |
| B-brief | builder | W2 | done | 23 passed | fixture brief 18 sentences, 2.0 pages. Unfaithful sentence 0/3 votes, one-word copy edit caught and restored |
| B-cli | builder | W2 | done | 21 passed (suite 334) | tail colors traffic, inject-errors 3/3 caught then re-cited. CLI uses urllib (own API only) |
| A-deploy | aws-operator | W2 | done | healthz, runs, CORS, test page, IMDSv2, role calls | EC2 i-0dec5cc5824a11aec behind API Gateway. Deploys go through S3 (`infra/deploy.sh`). Env changes via `infra/remote.sh --set-env` |
| B-g2fix | builder | W2 | done | suite 366 passed | skipped scan now quarantines, link-local blocked (metadata URL too), pre-approval policy shows system rules, fake runs labeled |
| B-testpage | builder | W2 | done | 41 passed | /test-pages/injection.html served by our API (simulated header). Full path tested: allowed, flagged, quarantined, link blocked |
| B-fix-brief | builder | R0 fix | done | suite 415, 1 live | Sonnet wrapped its whole answer in an extra string. Runner now unwraps it safely. Both R0 outputs replay as valid |
| B-fix-extract | builder | R0 fix | done | suite 441 | live: plan pages 100/100, r0 failure pages 148/150. Replaying r0 outputs: 48 fails down to 6 (98.3% pass or fixed). $0.42 |
| B-fix-draft | builder | R0 fix | done | 35 passed (suite 442), 1 live | live on R0 data: 5 of 6 scored, ROTIC -1 at 9.8% (3 of 10 years, low confidence), all 12 conclusions pass or fixed. Tuning spend about $9 |
| B-fix-repair | builder | R0 fix | done | 50 passed (suite 457) | code's threshold score wins (9.996% keeps -1), judgment scores only move toward 0 or N/A, blocked items never repaired |
| B-live | builder | W3 | done | 131 passed, 2 live rehearsals | every A10 beat PASS live twice ($0.10): links blocked, S-006 revoke blocked, S-010 flagged and quarantined, STOP blocks. Commands in DEMO.md |
| B-fix-checks | builder | R0 fix | done | 75 passed (suite 465) | planted 13/13 caught, clean 13/13. Own clause beats headers, comparison bases, nil cells, exact threshold, adjacent-year averages. Old vs new on stored runs: 9 fail-to-pass (all right), 0 pass-to-fail |
| B-baseline | builder | W3 | done | 13 passed, 1 live | harness caught 24/25 planted errors. Plain Claude: 59 claims, quotes all exact, but its scores went unchecked (invented coverage history, FCF < maturities called "exceeds"). $0.56 |
| B-p1 | builder | W3 | todo | | |

## Gates

| Gate | Result | When | Notes |
|---|---|---|---|
| G0 Preflight | pass | Sat 7:15 PM | guard live, 80/80 guard cases, account ok, venv py3.12 (uv), main already merged |
| G1 Scaffold and AWS | pass | Sat 7:29 PM | requirements install, imports ok, 26 event types, pytest 8 passed, .env has every .env.example key, AWS_RESOURCES.md has delete commands, concurrency 32/8 |
| G2 Core | pass | Sat 8:15 PM | healthz live, contract_smoke 51/51 vs real API, check_plan PASS (S-010 warn), ingest_one S-001 ok, pytest 160 pass. planted 12/12 caught, clean 12/12, pytest 227 pass. Verifier pass: e2e probe shows revoke, link block and quarantine as in the demo. 1 major (skipped scan fails open) going to B-g2fix |
| G3 Pipeline and pre-smoke | partial | Sat 8:32 PM | pytest 366. Live presmoke (S-001, 5 pages): every stage ok, 74 items, 48/53 data pass (5 fail = % column known issue, became holes), 10 holes, brief final 32 sentences 2.9 pages, $0.80, 110 s. contract_smoke --run 21/21. CLI run/approve/revoke/stop/tail ok. Verifier running |
| G4 Deploy | pass | Sat 10:10 PM | public healthz live, /api/v1/runs lists, test page 200 with simulated header, deploy.sh moved the instance to b223c2b (the pushed HEAD) |
| G5 Full SHL run, demo-ok-1 | | | |
| G6 Demo rehearsal | | | |

## AWS

See backend/AWS_RESOURCES.md. Model ids, concurrency and the invoke URL go here once known.

- **Invoke URL: https://ppgcikvoi0.execute-api.us-east-1.amazonaws.com** (API at `/api/v1`, health at `/healthz`, test page at `/test-pages/injection.html`).
- Deploy: `bash backend/infra/deploy.sh` ships the committed HEAD through S3 and prints the deployed sha. `bash backend/infra/remote.sh --set-env KEY=VALUE` changes `/etc/axelrod.env` and restarts the service. Logs: `bash backend/infra/remote.sh 'journalctl -u axelrod --no-pager -n 50'`.

- Models: large `us.anthropic.claude-sonnet-5`, small `us.anthropic.claude-haiku-4-5-20251001-v1:0`. Both pass a forced tool call.
- Concurrency: 50 parallel Haiku 50/50 ok, p95 931 ms. 8 parallel Sonnet 8/8 ok, p95 1.5 s. No throttles. `.env`: MAX_CONCURRENCY_SMALL=32, MAX_CONCURRENCY_LARGE=8.
- Prices (USD per Mtok, us.* regional): Sonnet 5 2.20 in / 11.00 out, Haiku 4.5 1.10 / 5.50.
- Guardrail `axelrod-tripwire` v1: injection sample INTERVENED, clean page NONE.
- Storage: S3 `axelrod-vault-mfafxo`, DynamoDB `axelrod-runs` and `axelrod-events`. IAM role and instance profile `axelrod-ec2` created.
- Invoke URL: not yet (A-deploy, W2).

## Decisions

- Sat 10:10 PM: the orchestrator runs `infra/deploy.sh` for redeploys (S3 upload plus SSM restart, no new resources). Creating or changing AWS resources stays with the aws-operator.

- Sat 9:17 PM: keep the extractor's skip of % change table columns. With the current `_period_text`, a % cell in a row of amounts passes for either year, so year claims there would go unchecked.

- Sat 8:49 PM: for the live demo, set `DEMO_PACING=2` and `MAX_CONCURRENCY_SMALL=8` on the instance (fewer metered calls discarded at STOP). Keep 32 for the full SHL run.

- Sat 7:15 PM: start S1 right after G0 instead of at 8:00 PM (Gordon's call). Cutoffs stay at their clock times.
- Sat 7:15 PM: venv is Python 3.12 via uv (no pip inside, so installs use `uv pip install --python .venv/Scripts/python.exe`). Code targets 3.11 for EC2.
- Sat 7:16 PM: no git identity on this machine. Commits use `git -c user.name=Claude -c user.email=noreply@anthropic.com`, matching the earlier backend commits. Git config is not changed.
- Sat 7:33 PM: Gordon's call: from now on commits are authored only as `tyang4-ai` (repo-local git config, GitHub no-reply email), with no Claude co-author line. S0 and G1 stay as pushed (rewriting them would need a force push).
- Sat 7:25 PM: build on Sonnet 5 (large) and Haiku 4.5 (small), since the 5.5 models are denied. Swap is one `.env` change plus `infra/ec2_role.py` if organizers allow 5.5.

- Sat 7:31 PM: Gordon is away for a while. Bash is allowed without prompts in `.claude/settings.local.json` (personal, not committed). The guard hook and the deny rules still apply.

- Sat 7:53 PM: accepted B-shl-plan's choices. `check_plan.py` uses urllib (a dev check, not runtime), and S-002 is the release PDF.

- Sat 7:58 PM: Gordon's call: the repo stays private until submission. Deploys copy the code through the S3 vault bucket (no git clone on EC2). Our API serves the planted test page (new plan B-testpage).
- Sat 7:58 PM: Gordon's call: don't wait at gates. W2 starts now, while B-verify finishes and G2 runs alongside. No W2 plan owns a B-verify file.

## Known issues

- Live spend so far is about $15 (R0 $1.20, B-fix-draft tuning about $9, plus presmokes and rehearsals), all in the workshop account.

- Revoke cascade: an item whose checks are running in a worker thread when the revoke lands can still be recorded as pass. After a revoke on a finished run, report and brief certainty and the rollup stay as finalize left them (items and brief stats do update).

- Windows only: with a long DATA_DIR (for example a temp folder), vault paths for S-002 and S-009 pass the 260-character MAX_PATH limit and their ingest fails. The default `backend/.data` is fine, and EC2 is Linux. Keep DATA_DIR short on the laptop.
- Presmoke cost on 5 pages: $0.80 (146 model calls). The full run reads 36 PDF pages plus HTML and JSON, so expect about $3 to $5 against `BUDGET_USD=5`. Raise the budget for R1 if the estimate holds.

- `verify/checks.py` `_period_text`: in a mixed table row (amounts plus % change columns), % tokens are aligned to the year header, so a % change column reads as the prior year. For now the extractor skips % change columns. Growth rates written in a sentence are still extracted. Fix in W3 if time allows.

- An analyst edit (`PATCH /items`) re-runs the code checks but keeps the old judge votes, marked "From before the analyst edit". It doesn't update holes or the rollup.

- G2 minor, left as is: allow rules ignore scheme and port, and the path prefix has no segment boundary (same host only, low risk). Cost in event messages uses `:.4f` (not a report value).
- The workshop account id is in RUNBOOK.md, PLANS.md and `.claude/agents/aws-operator.md`. AWS doesn't treat account ids as secret, and the account goes away after the event, but scrub them before the repo goes public if there's time.

- The gateway now blocks link-local hosts (B-g2fix), but a DNS name that resolves to a link-local address isn't caught. EC2 requires IMDSv2, which covers it.

- Verify limit: a dash followed by a space before a number ("– 9,871") could be a minus or a range dash, so a dropped minus is not caught there. Negatives in parentheses are always strict.

- Guard quirk: `git push origin backend 2>&1 | tail -1` is read as a push to "2>" and asks Gordon. Orchestrator pushes are plain `git push origin backend` from now on. The guard itself is unchanged (Gordon's file).

## Needs Gordon

- (10:05 PM, pass to Tong now) **VITE_API_URL=https://ppgcikvoi0.execute-api.us-east-1.amazonaws.com/api/v1**. It serves the recorded R0b run (mode replay) right now, and the real SHL run (G5) shortly.
- (10:05 PM, decision) The deployed API has no key, so anyone with the URL can start runs that spend Bedrock money (capped at $5 per run, not in total). Setting `AXELROD_API_KEY` would make Tong's UI send an `X-Axelrod-Key` header on every call. My default: no key for now, since the URL is only shared with the team. Say "set a key" and I'll set it and tell you the value to pass to Tong.
- (10:05 PM, not blocking) The API Gateway integration points at the instance's public DNS, which changes if the instance is ever stopped and started. Option: add an Elastic IP (not in the plan). Without one, the fix is one `apigatewayv2 update-integration` command, written in AWS_RESOURCES.md. My default: leave it, and don't stop the instance.

- (9:46 PM, pass to Tong) A real, finished SHL run now exists as a replay: `backend/data/replay/run_20261108_whhj0d` on the backend branch. Any backend started from this branch serves it as `mode: replay` (labeled recorded), so Tong can build the report, brief, document and vault panels against real data before the EC2 URL exists. Run it with `STORE=local python -m uvicorn backend.axelrod.api:app --port 8001`, then point VITE_API_URL at it.

- (9:22 PM, demo script) **ROTIC scores -1, not +1.** On our SHL data the ROTIC proxy is a 9.8% mean (FY2023 8.4%, FY2025 11.0%, FY2026 10.0%), just under the 10% bar, and only 3 of 10 fiscal years have the inputs, so certainty is low. vs peers (FY2025): SHL 11.0%, GE HealthCare 25.2%, Philips 18.1%, so that also scores -1. The builder did not tune toward a score, and we shouldn't. docs/PLAN.md's beat "ROTIC scores +1, but with low certainty" needs new words. Suggested: "ROTIC scores -1, and we flag low certainty: only three of ten years since listing have the inputs. We tell you how much to trust each line, and why." The final R1 run may differ a little, so I'll confirm the numbers after it.

- (8:52 PM, pitch wording) Baseline result (`backend/data/baseline/results.md`, labeled experiment): a plain Claude report's **quotes were all exact** (0 of 59 failed our quote check). Don't say "plain Claude makes up numbers". The honest line: on the same pages, the plain report's **reasoning and scores went unchecked**. It scored research_stage +1 from an invented coverage history and called €2.7bn of free cash flow enough to cover €6.2bn of maturities. 12 of its 59 claims also fail the period check. Our checks caught 24 of 25 planted errors in the harness report. There are also 20 key numbers in results.md for you to hand-check.

- (8:49 PM, contract change, additive) Add `"source_revoked"` to `ItemUpdatedData.reason` in `contract/models.py` (it allows repair, comment, user_edit, demo_injection today). Why: when a source is revoked mid-run, the items citing it turn blocked, and the cascade should say why in an `item.updated` event. Until then it sends `item.verified` only, which already carries the blocked status and the detail "Source revoked: S-006". The backend switches over by itself once the reason exists. Say yes and I'll make the change on main at a gate (models.py, types.ts, mock, CHANGELOG, VERSION), then you tell Tong.

- (8:18 PM, pass to Tong) The CLI prints a run link as `PUBLIC_UI_URL?run=<run_id>` (or `PUBLIC_UI_URL` with `{run_id}` replaced). The UI has no run deep link yet. If Tong adds one, tell me the URL shape and I'll set `PUBLIC_UI_URL`.

- (8:17 PM, pass to Tong) Brief UI behavior: when a brief sentence is repaired, its new text arrives in the `summary.verified` event and the `/summary` refetch, not in a second `summary.sentence` event. A sentence that fails twice is removed, and it disappears at the `summary.ready` refetch. Blocked sentences stay, marked blocked.

- (7:49 PM, not blocking) The reviser role gets the small model in PLANS.md but Sonnet in `mock/build_fixtures.py` ROLE_SCOPES. The real gateway follows PLANS.md. Either change the mock on main or say "reviser uses Sonnet" and I'll switch the backend. The reviser is P1 (W3), so this can wait.


- (7:25 PM, not blocking) The workshop blocks Claude Sonnet 5.5 and Haiku 5.5 (policy `ws-bedrock-model-allowlist-policy-1`). We are building on Sonnet 5 and Haiku 4.5, which work. If you want 5.5, ask an ACM organizer to allow `anthropic.claude-sonnet-5-5` and `anthropic.claude-haiku-5-5`. Either way, Devpost and the pitch should name the models we really use.

## Log

- Sat 7:15 PM: G0 pass. Guard self-test denied as expected, test_guard 80/80, AWS account is the workshop one, `origin/main` b61ffa0 already merged, venv created with base packages.
- Sat 7:25 PM: A-setup done. All S1 AWS resources created and tested. 5.5 models denied, substitutes in .env.
- Sat 7:29 PM: P-scaffold done. G1 pass. Added 6 non-AWS default keys to .env (DATA_DIR, FAKE_MODELS, HTTP_USER_AGENT, AXELROD_API_KEY, PUBLIC_UI_URL, DEMO_PACING).
- Sat 7:30 PM: W1 launched (5 builders).
- Sat 7:53 PM: B-ingest, B-gateway, B-shl-plan done. Pushed plan.json and the planted test page so its raw URL answers.
- Sat 8:01 PM: W2 launched early (6 agents). G2 checks 3 to 5 pass. Verifier running on the 4 finished W1 plans.
- Sat 8:01 PM: B-verify done. W1 5/5. Planted test 12/12.
- Sat 8:15 PM: G2 pass (verifier). B-g2fix launched for the one major finding plus three minor ones.
- Sat 8:26 PM: B-brief, B-cli, B-extract, B-g2fix done (W2 5/7). B-live and B-baseline started early. G3 waits on B-reason.
- Sat 8:32 PM: B-reason done (W2 6/7, only A-deploy left). G3: live presmoke ok through every stage, $0.80. Verifier on W2 running.
- Sat 8:52 PM: B-baseline done ($0.56). Added S-002 p.6 to N-01 and N-06 in plan.json (FY2026 adjusted EBIT and free cash flow, found by the baseline).
- Sat 8:49 PM: B-live done. All demo beats pass live. DEMO.md written. Tail shows NEXT lines by default (orchestrator, cli.py), "Source revoked: S-006" capitalization fixed (checks.py).
- Sat 8:38 PM: rehearsal R0 done (failed at summarize). Live gateway beats ok. Launched three fix builders.
- Sat 9:42 PM: all R0 fixes in (brief, extract, draft, repair, checks). Started rehearsal R0b.
- Sat 9:46 PM: R0b full rehearsal done (94.5% items pass or fixed, brief 2.8 pages, $2.06). Exported and committed as the replay fallback.
- Sat 10:05 PM: A-deploy done (Gordon approved port 8000). G4 curl checks pass.
- Sat 10:10 PM: G4 pass. Starting G5 on EC2.
