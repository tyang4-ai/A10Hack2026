DEMO RUN: run_20261108_rzpoj7 (picked 1:32 AM)

- **Run:** R2 on EC2, 647 items, 96.6% pass or fixed, 5 of 6 criteria scored, rollup -0.6, certainty 94 (report) and 74 (brief), brief 2.5 pages, $2.84. Tag `demo-ok-2`, replay in `backend/data/replay/run_20261108_rzpoj7`. Backup: demo-ok-1 run_20261108_pin2ve. The comparison is under Now.
- **Deployed:** 4f10702 on EC2 (`axelrod` service, port 8000). Later commits are only this board, DEMO.md and the R2 replay, which EC2 doesn't need because a stored run wins over a replay with the same id. Invoke URL `https://ppgcikvoi0.execute-api.us-east-1.amazonaws.com`. Webapp: `NEXT_PUBLIC_API_BASE=<invoke-url>/api/v1`. No API key yet (Needs Gordon).
- **Server env for the demo** (set 1:33 AM in `/etc/axelrod.env`):
  - `CACHE_DOCUMENTS=on`: the gateway decides every fetch, then serves it from the S3 cache.
  - `CACHE_MODELS=record`: model calls stay live and each answer is also recorded, so the in-person rehearsals record the demo path for the replay fallback. Runs say `cache_mode: documents`.
  - `DEMO_PACING=0.8`: seconds before each fetch. In the scripted check, STOP landed at about 52 s with a 10 s approval, revoking S-006 right after the first fetches. Set what the in-person rehearsal needs: `bash backend/infra/remote.sh --set-env DEMO_PACING=<seconds>`, or ask this session.
  - `MAX_CONCURRENCY_SMALL=8`, `BUDGET_USD=1` (per run, since 1:42 AM: the live part costs about $0.10 to $0.18 up to STOP), `STORE=aws`, `GUARDRAIL_ID` set, `TEST_PAGE_URL=<invoke-url>/test-pages/injection.html`.
- **Fallback if Bedrock throttles:** `--set-env CACHE_MODELS=replay` serves recorded answers at $0, labeled `documents_and_models`. So far that's R2's 291 answers. It's untested on the live segment until a rehearsal has run with `record`.
- **Cut:** the scripted G6b rehearsal and B-rehearse (Gordon, 1 AM). The script stays in the repo, unused. Nothing else: v2 steps 1 to 12 are built.

# Backend build board

Kept by the orchestrator (backend/RUNBOOK.md section 5). Newest state at the top, history in Log.

## Now

- Stage: **v2** (docs/BACKEND_V2.md, contract v0.3.0, origin/main 69b40fc merged at 1:24 AM). Gordon's 1 AM change: the integrator runs next to us and shares EC2 (it owns `axelrod-web` on port 3000 and the `$default` route, we own the `axelrod` service on 8000 and its env). No scripted rehearsal, no READY FOR INTEGRATION. Cutoff: R2 done and the `DEMO RUN:` line posted by 3:15 AM.
- **R2 done** at 1:29 AM: run_20261108_rzpoj7. EC2 is free for runs again.
- **Env changed at 1:33 AM** (restarted the `axelrod` service only, no deploy): demo values `DEMO_PACING=0.8`, `MAX_CONCURRENCY_SMALL=8`, `BUDGET_USD=5`, `CACHE_MODELS=record`, `CACHE_DOCUMENTS=on`.
- **Deployed at 1:25 AM:** 4f10702 to EC2, `axelrod` service on port 8000 only. Then env for R2: `DEMO_PACING=0`, `MAX_CONCURRENCY_SMALL=32`, `CACHE_MODELS=record`, `CACHE_DOCUMENTS=on`, `BUDGET_USD=10`. `axelrod-web`, port 3000 and the `$default` route are not touched.
- **R2 against demo-ok-1** (same API, same counting, `r2_compare.py` in the scratchpad):

| | demo-ok-1 run_20261108_pin2ve | **R2 run_20261108_rzpoj7** |
|---|---|---|
| Code, corpus | v1, 10 sources, live fetches | v2, 16 sources (plan v2 plus the archive), `cache_mode` documents: 16 of 16 fetches from the cache |
| Items | 374 (348 data, 15 conclusions, 6 scores, 5 charts) | 647 (592 data, 41 conclusions, 6 scores, 8 charts) |
| Pass or fixed | 345 + 18 of 374 = **97.1%** | 570 + 55 of 647 = **96.6%** |
| Failed (declared holes) | 11 (13 holes) | 22 (24 holes): 19 data, C-018 and C-019 (tier 3 only), C-026 (a judge error, see Known issues) |
| Scores | rotic_level -1, rotic_vs_peers 0, liquidity -1. secular_growth and leverage failed. Research stage N/A | rotic_level -1, **rotic_vs_peers -1** (below GE HealthCare 24.6% and Philips 16.8%, text and sign agree), secular_growth +1, leverage -1, liquidity -1. Research stage N/A |
| Coverage | 3 of 6 | **5 of 6** |
| Rollup | -0.6 | -0.6 |
| Mean certainty, report / brief | 93 / 75 | 94 / 74 |
| Brief | 2.8 pages, 24 sentences | 2.5 pages, 18 sentences (3 fixed, none failed) |
| Cost, time | $1.96, 3.9 min, 197 model calls | $2.84, 3.4 min, 291 model calls (all recorded to `cache/models/`) |
| Bobby's calls | none (before v2) | plan proceed, extract rework (3 extractors), repair proceed, brief proceed |

  **Picked R2**: the same pass rate on 1.7 times the items, 5 of 6 criteria scored instead of 3, every score's sign agrees with its text, and it shows Bobby, the cache and the archive. demo-ok-1 stays as the backup (tag and replay kept). Exported to `backend/data/replay/run_20261108_rzpoj7` (47.6 MB, text only), tagged `demo-ok-2`.
  **For the pitch (docs/PLAN.md 1:20 to 2:35):** in R2 the judges rejected no score, and all five scorable criteria are scored, so "The judges rejected two scores" doesn't fit R2. Don't show C-026 as a caught error: its rejection is a judge error (Known issues). ROTIC still scores -1 with certainty flagged (54, four fiscal years computable).

## v2 plans

| Plan | Steps | Agent | Status | Tests | Notes |
|---|---|---|---|---|---|
| V1-shapes | 1 | builder | done | 34 passed (suite 661) | every v0.2/v0.3 field and event, Bobby first then "Bobby's goon #n", routes and stubs, cache keys. contract_smoke 82/82 mock, 70/70 real |
| V2-gateway | 2, 6 | builder | done | 113 passed, 3 live | stop one agent (R-stop rule after the kill switch, late results discarded, work becomes a hole), document cache (decide first, S3, hit/miss labeled, scan still runs). Live: archive object served from S3 |
| V3-rework | 3, 4, 5 | builder | done | 37 passed (suite 757), 1 live | rerun one agent (extractor, drafter, summarizer, repairer; rerun of a rerun), Bobby at 4 gates (code validates, $0.005 and 2.3 s per gate live), comments resolved by Bobby |
| V4-archive | 7 | builder + aws-operator | done | 16 passed | uploaded: archive/shl/ 930 objects (988 MB), cache/docs/ 928 entries. Idempotent rerun skipped everything. EC2 role reads and writes the cache already. LSEG and TED CSVs excluded |
| V5-plan2 | 8 | builder | done | 43 passed (suite 662) | S-011 to S-016: SHL annual reports FY2024, FY2022, FY2020, FY2018 (ten ROTIC years FY2017 to FY2026) and Philips 2025, 2023. 30 new pages, all checked against the PDFs. S-006 now fetches 14th, still before S-009 and S-010 |
| V6-resume | 9 | builder | done | 16 passed (suite 860) | resume only from stopped: cancels the old task with the kill switch still on, kill switch off (version bump), `run.resumed`, running. Restarts the cut stage, extract page by page, nothing finished redone. Analyst-stopped goons stay stopped, Bobby comes back unless R-stop-bobby. A terminal status is final (`revive_orchestrator` is the one exit, Bobby only) |
| V7-chat | 10 | builder | done | 23 passed, 2 live | chat with any goon or Bobby through the gateway, read only, labeled not checked. About $0.003 (Haiku) and $0.006 (Sonnet), 2 to 4 s |
| V8-fork | 11 | builder | done | 41 passed (suite 798) | rerun from a step (new run, own policy and log, ids kept, parent unchanged), `orchestrator.start_from`, model cache (`CACHE_MODELS=record/replay`, decision first, a hit costs $0, the n-th identical call gets its own record) |
| pre-R2 | 12 | orchestrator | done | | run_20261108_5zqdvb on EC2, cache_mode documents, 16 sources (13 cache hits, 10 from the archive). 659 items (522 pass, 88 fixed, 49 fail = 92.6%), 52 holes, certainty 93, rollup +0.4 (4 of 6: rotic_level 0, rotic_vs_peers +1, secular_growth +1, leverage -1), brief 2.8 pages, $3.03, 6 min. Bobby: plan escalate, extract rework (S-015 extractors), repair proceed, brief proceed. **Bug: rotic_vs_peers +1 while its text says SHL trails both peers.** Fixes: B-fix-score, B-fix-s015 |
| B-fix-score | 12 | builder | done | 93 passed (suite 824), 3 live | judges check a score's direction against its text and rubric (`score_direction`). Live on r2a rotic_vs_peers +1: 0 of 3, all naming "trails both peers". Same text at -1: 3 of 3. Bobby's plan gate proceeds on the r2a digest. $0.034 live |
| B-fix-score2 | 12 | builder | done | 15 passed (suite 879) | wire the drafter's direction pre-check, swap every drafter key for its id (E-002 kept "(c_sh_fy2025_rotic)"), plan-gate digest lists criteria with no need and needs with no source |
| B-rehearse | 12 | builder | done, then cut | 38 passed | demo_live.py `--webapp`: reorder and reject at approval, cache labels, stop one goon, launch to STOP at most 55 s, suggested DEMO_PACING |
| B-fix-s015 | 12 | builder | done | 54 passed (suite 880) | Philips year headers after the rows, unit lines without brackets, maturity balance dates. Replay of r2a: S-015 fails 35 to 0 (10 new passes checked by eye). S-014's 8 maturity fails went to B-fix-due |
| B-fix-due | 12 | builder | done | 44 passed (suite 924) | period_match: a due year in the value's own clause is never its period (today a due-year claim passes falsely), and a range header cell counts as one column |
| R2 | 12 | orchestrator | todo | | full run on the full corpus, compared with demo-ok-1 here |
| G6b | 12 | orchestrator | cut | | Gordon, 1 AM: we demo in person. Scripted rehearsals had already run (Gates). DEMO_PACING stays an env setting |

## v1 plans (all done)

P-scaffold, A-setup, B-store-api, B-gateway, B-verify, B-ingest, B-shl-plan, B-extract, B-reason, B-brief, B-cli, A-deploy, B-g2fix, B-testpage, B-live, B-baseline, B-fix-brief, B-fix-extract, B-fix-draft, B-fix-repair, B-fix-checks, B-fix-cite, B-fix-holes, B-fix-polish. B-fix-yield partial (3 of 5 scored in its one full live run, composite 4 of 5; committed, R2 is the real test). B-fix-judge done (judges see signed values; period_match checks statement years, replay flips only G5b D-262, a real mismatch). Not started: B-p1. Details are in PLANS.md "Interface notes" and in git history.

## Gates

| Gate | Result | When | Notes |
|---|---|---|---|
| G0 to G2 | pass | Sat 7:15 to 8:15 PM | guard live, AWS set up, core verified (contract smoke 51/51, planted errors caught) |
| G3 Pipeline | fail at 1c7118c, then fixed | Sat 10:17 PM | blocker (brief crash) and both majors (quote vs span, brief of hole bullets) fixed by B-fix-brief, B-fix-cite, B-fix-holes |
| G4 Deploy | pass | Sat 10:10 PM | EC2 behind API Gateway, deploy.sh ships the pushed HEAD |
| G5 Full SHL run | pass, **demo-ok-1** | Sat 10:15 PM | run_20261108_pin2ve: 374 items, 345 pass, 18 fixed, 11 fail, 13 holes, certainty 93, rollup -0.6 (3 of 6), brief 2.8 pages, $1.96, 4 min |
| G5b | not tagged | Sat 11:02 PM | run_20261108_0murfv: data better, but 1 of 6 scored (coarse conclusions). Fix: B-fix-yield |
| G6 rehearsal 1 | pass | Sat 10:20 PM | CLI live segment on EC2, every beat PASS, $0.10. Superseded by G6b (webapp, 3 minutes) |
| G6b scripted rehearsals | pass, then cut | Sun 1:10 to 1:20 AM | Ran before Gordon's 1 AM message reached us. `demo_live.py --webapp` on EC2 at 95c55df, cached documents, live models. DEMO_PACING 2: every beat but timing (STOP 71.5 s). 0.6: all PASS, STOP 42.5 s. 1.0: all PASS, 50.7 s. **0.8 with a 10 s approval: all PASS, 51.7 s** (run_20261108_5hddrr). About $0.18 each. Revoke S-006 right after the first fetches. DEMO.md is now a click guide with these timings |

## AWS

See backend/AWS_RESOURCES.md (resources, delete commands, credentials).

- **Invoke URL: https://ppgcikvoi0.execute-api.us-east-1.amazonaws.com** (API at `/api/v1`, health at `/healthz`, test page at `/test-pages/injection.html`). Frontend: `NEXT_PUBLIC_API_BASE=<invoke-url>/api/v1`.
- Deploy: `bash backend/infra/deploy.sh` (committed HEAD through S3, prints the deployed sha). Env: `bash backend/infra/remote.sh --set-env KEY=VALUE`. Logs: `bash backend/infra/remote.sh 'journalctl -u axelrod --no-pager -n 50'`.
- EC2 env now (12:30 AM, full-run settings): `CACHE_DOCUMENTS=on`, `BUDGET_USD=10`, `MAX_CONCURRENCY_SMALL=32`, `DEMO_PACING=0`, `TEST_PAGE_URL=<invoke-url>/test-pages/injection.html`, `STORE=aws`. Deployed a4ab693 (contract 0.3.0). contract_smoke on EC2: 76/76.
- Models: large `us.anthropic.claude-sonnet-5`, small `us.anthropic.claude-haiku-4-5-20251001-v1:0` (the 5.5 models are denied by the workshop allowlist). Prices per Mtok: Sonnet 5 2.20 / 11.00, Haiku 4.5 1.10 / 5.50.
- Live spend so far: about $25, all in the workshop account (runs about $2 each, plus tuning).

## Decisions

- Sun 1:24 AM (Gordon's 1 AM message): integration runs next to us. No scripted rehearsals and no READY FOR INTEGRATION. When R2 is picked, the first line of this file becomes `DEMO RUN: <run id> (picked <time>)`. Every deploy is noted here before it runs. B-rehearse had already landed (ff105a8), so its script stays in the repo but nothing runs it. B-fix-due goes on, because it closes a false pass B-fix-s015 found.
- Sun 1:08 AM: the demo approval moves S-002 up above S-001 and rejects S-016 (Philips 2023: one page, 0 data items in the pre-R2 run, and S-008 carries those balances). The only non-priority sources are S-006, S-009 and S-010, which are never touched, so the rejection has to be a priority source. That makes S-006 the 13th fetch.
- Sun 1:08 AM: the demo approval moves S-002 up above S-001 and rejects S-016 (Philips 2023: one page, 0 data items in the pre-R2 run, and S-008 carries those balances). The only non-priority sources are S-006, S-009 and S-010, which are never touched, so the rejection has to be a priority source. That makes S-006 the 13th fetch.
- Sun 12:46 AM (Gordon may override): `CACHE_MODELS=record` labels a run `documents` (or `off`), since it replays nothing. Only replay sets `documents_and_models`, and replay with the document cache off says so too (the contract has no models-only value), so replayed answers are never hidden.
- Sun 12:46 AM: a fork keeps `R-stop-<id>` only for agents it copies (they never run again there, and the rule keeps their unfinished work a declared hole). Every other stop rule is dropped, `R-stop-bobby` included. A fork's Bobby doesn't get the rerun instructions: they're for the goons that redo work.
- Sun 12:28 AM (Gordon may override): judges never see rerun instructions, so no instruction can steer a check, and Bobby can't use a judge rerun to pass a claim.
- Sun 12:28 AM: pre-R2 on EC2 now with the committed code (steps 1 to 8 and 10, no fork or resume, which don't change a normal run), so problems show before the final R2.

- Sat 11:48 PM: the archive upload excludes the licensed LSEG files and the provenance-less TED CSVs (conservative default, Gordon may override). The upload runs after V1 lands, because the cache keys come from V1's `cache.py`.

- Sat 11:35 PM: v2 runs in three waves, so no file has two owners: V1 alone (plus V4 and V5, which touch other files), then V2, V3 and V7, then V6 and V8. V1 puts every new route in `api.py` as a thin call into its own module, so later plans never share `api.py`.
- Sat 11:35 PM: the archive rule. Gordon's summer archive feeds runs as primary source documents only (his explicit call in docs/BACKEND_V2.md). No script, note or analysis from `Notes/Finance/` is ever opened, copied or run. Sidecars and indexes are read only to find each file's original URL and date.
- Sat 10:55 PM (Gordon may override): a cited cost or outflow line may be stated as its size ("Income tax expenses were €646 million" for -646). Every other negative needs a minus, brackets or a sign word.
- Sat 10:41 PM: the brief always carries code's gap summary. Offline tests ignore the recorded replays (conftest).
- Sat 10:10 PM: the orchestrator runs `infra/deploy.sh` and `infra/remote.sh --set-env`. Creating or changing AWS resources stays with the aws-operator.
- Sat 9:22 PM: ROTIC is scored from the years that have inputs, and code lowers certainty. Nobody steers a score.
- Sat 7:58 PM: the repo stays private until submission (Gordon). EC2 never clones it. Code ships through S3, and the test page is served by our API.
- Sat 7:33 PM: commits are authored only as `tyang4-ai` (no Claude co-author line).
- Sat 7:31 PM: Bash runs without prompts (`.claude/settings.local.json`, personal). The guard and the deny rules still apply.
- Sat 7:25 PM: Sonnet 5 and Haiku 4.5, since the 5.5 models are denied.

## Known issues

- **C-026's rejection in R2 is a judge error.** C-026 says €488 million at September 30, 2025 and €592 million at September 30, 2024, exactly as D-413 (FY2025) and D-424 (FY2024) state. Two of three judges said the years were reversed. It shows as a failed conclusion and a hole, so nobody shows it as a caught error.
- Extract starts only after collect finishes (stages run one after another), so in the demo the goon stop comes after S-010. The server's DEMO_PACING isn't reported anywhere.
- Extract starts only after collect finishes (stages run one after another), so in the demo the goon stop comes after S-010. The server's DEMO_PACING isn't reported anywhere, so demo_live measures it from the events.
- Resume inside draft asks a drafter again if it finished inside the STOP cut, because draft builds items only after every drafter answers. A few cents, no wrong data.
- Plan v2 has 13 priority sources (was 7), and 6 more paced fetches come before S-006. Retune `DEMO_PACING` at G6b for the 55 s launch-to-STOP. The ROTIC drafter group should be about 310 rows against MAX_ROWS=400.

- Runs vary: R0b scored 4 of 6, pin2ve 3 of 6, G5b 1 of 6 (coarse conclusions, being fixed).
- Revoke cascade: an item mid-check when a revoke lands can still be recorded as pass. After a revoke on a finished run, report certainty and the rollup stay as finalize left them.
- Windows only: a long DATA_DIR pushes vault paths past MAX_PATH (260). Keep DATA_DIR short (`backend/.data/<name>`). EC2 is fine.
- The extractor skips % change table columns (year alignment). Growth rates in sentences are still extracted.
- Verify limit: a spaced dash before a number ("– 9,871") counts as either sign.
- An analyst edit (`PATCH /items`) keeps the old judge votes (marked) and doesn't update holes or the rollup.
- Allow rules ignore scheme and port, and the path prefix has no segment boundary (same host only).
- The gateway blocks link-local hosts, but a DNS name resolving to one isn't caught (EC2 requires IMDSv2).
- The workshop account id is out of RUNBOOK.md, PLANS.md and `.claude/agents/aws-operator.md` (1:45 AM). They point at `AWS_EXPECTED_ACCOUNT` in backend/.env, which is gitignored. The deploy scripts still check `AWS_ACCOUNT_ID` in the same file (same value). It's still in `docs/INTEGRATE.md` line 29 (main, not ours) and in git history (not rewritten, Gordon's call).
- Guard quirk: a redirect or pipe after `git push` (or any `>`, `2>&1` in a git command) is read as a path or push target. Use plain git commands.

## Needs Gordon

- (1:03 AM, mock, not blocking) On resume we emit `policy.updated`, `run.resumed`, `run.status` running, as CONTRACT.md 13 reads. `mock/mock_server.py` emits `run.resumed`, `run.status`, `policy.updated`. The frontend shouldn't care about the order. Say which way and I'll align the mock or the doc on main.

- (12:13 AM, contract, additive, not blocking) `AgentMessage` has no field for why a chat reply failed, so the reason lives only in the `agent.message` event's message line. An optional `error: str | None` would let the UI show it. Say yes and I'll add it on main at a gate.

- (11:48 PM, archive) The importer leaves out **100 LSEG transcripts and slides**, whose sidecars say "Licensed third-party content. Private research use; do not redistribute", plus **3 TED-EU CSVs** with no sidecar, URL or date (possibly derived data). My default: both stay out of S3, since the demo shows vault files to judges. Say "include LSEG" to add them with `--include-licensed` (1,029 files, 1.09 GB).

- (11:45 PM, not blocking) Facts without a number ("strong growth in fiscal year 2025") are not year-checked, because their statements add the report's own year, which no quote repeats (checking them wrongly flagged 13 items). Say if you want them checked against the source's period instead.

- (11:35 PM, decision, asked again by docs/INTEGRATE.md) **API key** on the deployed API? Today anyone with the URL can start runs (capped at $5 each). With a key, the webapp sends `X-Axelrod-Key` from `NEXT_PUBLIC_AXELROD_KEY`. My default: no key until you say so.
- (10:05 PM, not blocking) No Elastic IP: the API Gateway integration points at the instance's public DNS, so don't stop the instance. The one-line fix is in AWS_RESOURCES.md.
- (7:49 PM, not blocking) The reviser role is on the small model in the backend but on Sonnet in `mock/build_fixtures.py` ROLE_SCOPES. Pick one.

## Log

- Sat 7:15 to 8:15 PM: G0, G1, G2 pass. W1 built (store, API, gateway, verify, ingest, frozen plan).
- Sat 8:00 to 9:00 PM: W2 built (extract, reason, brief, CLI, test page, deploy). G3 live presmoke ok.
- Sat 8:38 PM: rehearsal R0 failed at summarize. Five fix builders followed.
- Sat 9:46 PM: rehearsal R0b done (94.5% pass or fixed, 4 of 6 scored). Committed as a replay.
- Sat 10:05 to 10:20 PM: A-deploy done (Gordon approved port 8000). G4 pass. G5 pass, run_20261108_pin2ve, tagged demo-ok-1. G6 rehearsal 1 pass.
- Sat 10:17 PM: G3 verifier findings, fixed by B-fix-cite, B-fix-holes, B-fix-polish.
- Sat 11:02 PM: G5b, 1 of 6 scored. B-fix-yield launched.
- Sat 11:32 PM: merged origin/main (contract v0.3.0). Started v2: V1-shapes, V4-archive, V5-plan2.
- Sat 11:36 PM: B-fix-yield partial and committed. B-fix-judge started.
- Sat 11:58 PM: V1-shapes done and committed. Wave 2 launched: V2, V3, V7 and the archive upload.
- Sun 12:00 AM: V5-plan2 done and committed (6 archive sources, 30 pages).
- Sun 12:01 AM: archive uploaded to S3 (929 files plus manifest, 928 cache entries).
- Sun 12:13 AM: V7-chat done and committed.
- Sun 12:20 AM: V2-gateway done and committed. V8-fork started.
- Sun 12:28 AM: V3-rework done and committed. contract_smoke picks a goon with a model for the rerun check.
- Sun 12:30 AM: deployed a4ab693. contract_smoke 76/76 on EC2. Pre-R2 run run_20261108_5zqdvb started (cache_mode documents, 16 sources).
- Sun 12:37 AM: pre-R2 run done on EC2 ($3.03, 6 min, 4 of 6 scored). Found a judgment score whose sign contradicts its text. Launched B-fix-score and B-fix-s015.
- Sun 12:46 AM: V8-fork done and committed (fork, start_from, model cache). Two stale stub tests updated. V6-resume started.
- Sun 12:50 AM: B-rehearse started (webapp beats for the G6b rehearsal).
- Sun 12:58 AM: B-fix-score done and committed. B-fix-score2 started (its three follow-ups, before R2).
- Sun 1:03 AM: V6-resume done and committed. Its gates.py follow-up (only analyst-stopped goons count as stopped by the analyst) went to B-fix-score2.
- Sun 1:08 AM: B-fix-score2 and B-rehearse done and committed. test_bobby given a real analyst stop rule (gate glue).
- Sun 1:13 AM: B-fix-s015 done and committed. B-fix-due started.
- Sun 1:20 AM: scripted G6b rehearsals on EC2: four runs, the last three all PASS. DEMO_PACING=0.8 recommended. DEMO.md rewritten for the webapp.
- Sun 1:24 AM: Gordon's 1 AM change of plan received. origin/main 69b40fc merged. G6b cut, B-rehearse cut (already committed). No READY FOR INTEGRATION: the DEMO RUN line instead.
- Sun 1:24 AM: B-fix-due done and committed (S-014 maturity fails 8 to 0, S-012 7 to 1, one old false pass now fails). Offline suite 924 passed. Deploy and R2 noted here first.
- Sun 1:26 AM: deployed 4f10702 (env for R2 set). R2 started: run_20261108_rzpoj7.
- Sun 1:29 AM: R2 done: run_20261108_rzpoj7, 647 items, 96.6% pass or fixed, 5 of 6 scored, $2.84, 3.4 min.
- Sun 1:32 AM: picked R2 over demo-ok-1 (table in Now). Exported as a replay, tagged demo-ok-2.
- Sun 1:33 AM: demo env set on EC2. DEMO RUN line posted: run_20261108_rzpoj7.
- Sun 1:42 AM: BUDGET_USD=1 on EC2 (Gordon, 1:45 AM message: no API key, so this caps each run).
- Sun 1:47 AM: account id moved to backend/.env (AWS_EXPECTED_ACCOUNT). C-026 logged as a judge error. On call until the 5:30 freeze: DEMO_PACING after the first in-person rehearsal, then one replay-fallback test.
