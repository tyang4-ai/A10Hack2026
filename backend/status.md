# Backend build board

Kept by the orchestrator (backend/RUNBOOK.md section 5). Newest state at the top, history in Log.

## Now

- Stage: **v2** (docs/BACKEND_V2.md, contract v0.3.0, merged at 11:32 PM). Cutoffs: steps 1 to 6 by 1:45 AM, 7 to 10 by 2:30 AM, R2 by 3:15 AM, G6b and READY FOR INTEGRATION by 4:00 AM.
- Running: B-fix-s015, B-rehearse, B-fix-score2.
- Next: deploy, then R2 on EC2 (`CACHE_MODELS=record`) and the G6b webapp rehearsal.
- Demo run so far: **demo-ok-1 = run_20261108_pin2ve** (EC2, live, 97.1% items pass or fixed, 3 of 6 scored). R2 is compared with it in this file before anything is picked.

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
| B-fix-score2 | 12 | builder | running | | wire the drafter's direction pre-check, swap every drafter key for its id (E-002 kept "(c_sh_fy2025_rotic)"), plan-gate digest lists criteria with no need and needs with no source |
| B-rehearse | 12 | builder | running | | demo_live.py `--webapp`: reorder and reject at approval, cache labels, stop one goon, launch to STOP at most 55 s, suggested DEMO_PACING |
| R2 / G6b | 12 | orchestrator | todo | | full run on the full corpus, then the 3-minute webapp rehearsal |

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

## AWS

See backend/AWS_RESOURCES.md (resources, delete commands, credentials).

- **Invoke URL: https://ppgcikvoi0.execute-api.us-east-1.amazonaws.com** (API at `/api/v1`, health at `/healthz`, test page at `/test-pages/injection.html`). Frontend: `NEXT_PUBLIC_API_BASE=<invoke-url>/api/v1`.
- Deploy: `bash backend/infra/deploy.sh` (committed HEAD through S3, prints the deployed sha). Env: `bash backend/infra/remote.sh --set-env KEY=VALUE`. Logs: `bash backend/infra/remote.sh 'journalctl -u axelrod --no-pager -n 50'`.
- EC2 env now (12:30 AM, full-run settings): `CACHE_DOCUMENTS=on`, `BUDGET_USD=10`, `MAX_CONCURRENCY_SMALL=32`, `DEMO_PACING=0`, `TEST_PAGE_URL=<invoke-url>/test-pages/injection.html`, `STORE=aws`. Deployed a4ab693 (contract 0.3.0). contract_smoke on EC2: 76/76.
- Models: large `us.anthropic.claude-sonnet-5`, small `us.anthropic.claude-haiku-4-5-20251001-v1:0` (the 5.5 models are denied by the workshop allowlist). Prices per Mtok: Sonnet 5 2.20 / 11.00, Haiku 4.5 1.10 / 5.50.
- Live spend so far: about $25, all in the workshop account (runs about $2 each, plus tuning).

## Decisions

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
- The workshop account id is in RUNBOOK.md, PLANS.md and `.claude/agents/aws-operator.md`. Scrub before the repo goes public if there's time.
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
