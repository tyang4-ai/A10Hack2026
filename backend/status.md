# Backend build board

Kept by the orchestrator (backend/RUNBOOK.md section 5). Newest state at the top, history in Log.

## Now

- Stage: **v2** (docs/BACKEND_V2.md, contract v0.3.0, merged at 11:32 PM). Cutoffs: steps 1 to 6 by 1:45 AM, 7 to 10 by 2:30 AM, R2 by 3:15 AM, G6b and READY FOR INTEGRATION by 4:00 AM.
- Running: V3-rework, V8-fork.
- Next: V6-resume (after V3 and V8). Then R2 on EC2 and the G6b webapp rehearsal.
- Demo run so far: **demo-ok-1 = run_20261108_pin2ve** (EC2, live, 97.1% items pass or fixed, 3 of 6 scored). R2 is compared with it in this file before anything is picked.

## v2 plans

| Plan | Steps | Agent | Status | Tests | Notes |
|---|---|---|---|---|---|
| V1-shapes | 1 | builder | done | 34 passed (suite 661) | every v0.2/v0.3 field and event, Bobby first then "Bobby's goon #n", routes and stubs, cache keys. contract_smoke 82/82 mock, 70/70 real |
| V2-gateway | 2, 6 | builder | done | 113 passed, 3 live | stop one agent (R-stop rule after the kill switch, late results discarded, work becomes a hole), document cache (decide first, S3, hit/miss labeled, scan still runs). Live: archive object served from S3 |
| V3-rework | 3, 4, 5 | builder | running | | rerun one agent, Bobby at the gates, comments to Bobby |
| V4-archive | 7 | builder + aws-operator | done | 16 passed | uploaded: archive/shl/ 930 objects (988 MB), cache/docs/ 928 entries. Idempotent rerun skipped everything. EC2 role reads and writes the cache already. LSEG and TED CSVs excluded |
| V5-plan2 | 8 | builder | done | 43 passed (suite 662) | S-011 to S-016: SHL annual reports FY2024, FY2022, FY2020, FY2018 (ten ROTIC years FY2017 to FY2026) and Philips 2025, 2023. 30 new pages, all checked against the PDFs. S-006 now fetches 14th, still before S-009 and S-010 |
| V6-resume | 9 | builder | todo | | |
| V7-chat | 10 | builder | done | 23 passed, 2 live | chat with any goon or Bobby through the gateway, read only, labeled not checked. About $0.003 (Haiku) and $0.006 (Sonnet), 2 to 4 s |
| V8-fork | 11 | builder | running | | rerun from a step, model cache, orchestrator.start_from (cut first if late) |
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
- EC2 env now: `DEMO_PACING=2`, `MAX_CONCURRENCY_SMALL=8`, `TEST_PAGE_URL=<invoke-url>/test-pages/injection.html`, `STORE=aws`.
- Models: large `us.anthropic.claude-sonnet-5`, small `us.anthropic.claude-haiku-4-5-20251001-v1:0` (the 5.5 models are denied by the workshop allowlist). Prices per Mtok: Sonnet 5 2.20 / 11.00, Haiku 4.5 1.10 / 5.50.
- Live spend so far: about $25, all in the workshop account (runs about $2 each, plus tuning).

## Decisions

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
