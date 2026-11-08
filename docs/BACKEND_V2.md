# Backend v2: agent controls, document cache, 3-minute demo

For the backend orchestrator. Gordon approved all of this on Sat Nov 7 at 11 PM. Start with `git fetch origin && git merge origin/main` (contract v0.2.0), then read `docs/CONTRACT.md` section 13 and `contract/CHANGELOG.md`. Turn the work below into plans in `backend/PLANS.md` with file ownership, as before. Your runbook rules still hold.

## What Gordon decided

1. **Tong's four requests are approved:** stop one agent, agent chat, resume after STOP, rerun from a step. The shapes are in contract v0.2.0. The mock answers all of them, so check behavior there first.
2. **`source_revoked` is approved** (`item.updated` reason). Switch the revoke cascade over to it.
3. **The demo runs on cached documents.** Gordon's summer 2026 archive of SHL source documents goes into S3, plus copies of everything Axelrod fetched tonight. The gateway still decides every request, then serves the body from the cache. Everything cached is labeled (`Run.cache_mode`, `gateway.request.cache`, `Document.origin`).
4. **The demo is 3 minutes, webapp first.** The CLI is on screen for about 10 seconds. Script: `docs/PLAN.md`, "Demo script: 3 minutes".
5. **When you finish, a second Claude Code session integrates and deploys** (`docs/INTEGRATE.md`). You hand over with READY FOR INTEGRATION (step 11).
6. **Credentials are logged.** Any credential you create or use (an API key, a token, a key pair) gets a line in `backend/AWS_RESOURCES.md` under "Credentials": name, where it lives, how to revoke it. Never the value. Gordon deletes everything right after the hackathon.

## Work, in order

**1. Wire the v0.2 shapes.** Run `parent_run_id`, `rerun_from`, `instructions`, `cache_mode`. `Document.origin` and `collected_at`. `GatewayRequestData.cache` and `cached_at`. `PolicyRule.agent_id` and origin `user_agent_stop`. `AgentStatusData.reason`. Events `agent.message` and `run.resumed`. Then `contract_smoke.py` against the real API must still pass, and should cover the new routes.

**2. Stop one agent.** `POST /runs/{id}/agents/{agent_id}/stop`. In `decide()`, right after the kill switch: a request from a stopped agent is denied by `R-stop-<agent_id>`. A result that lands after the stop is discarded, as with STOP. The run keeps going. The work that agent owned (its pages or items) becomes a declared hole, "agent stopped by the analyst", never silently missing. Tests: the next request is blocked, the others aren't, the hole exists, 409 and 404 cases.

**3. Resume.** `POST /runs/{id}/resume`, only from `stopped`. Kill switch off, policy version bump, `run.resumed` then `run.status` running. The pipeline restarts the stage STOP cut into and skips work that already finished (pages already extracted, items already verified). Agents stopped one by one stay stopped. Tests.

**4. Agent chat.** `GET` and `POST /runs/{id}/agents/{agent_id}/messages`, ids `AM-001`. The reply comes from that agent's role and model, through the gateway (kill switch, stop rule and budget apply), with the agent's task, inputs and transcript as context. Chat is read-only: it never changes the run, the report or the brief, and the UI labels replies as not checked. Events: the reply, then the analyst's message again as `answered` (or `failed`, for example when the agent is stopped). Keep replies short (at most about 120 words).

**5. Rerun from a step.** `POST /runs/{id}/rerun` with the table in CONTRACT.md section 13. Fork the parent's state up to `from_stage` into a new run, keep item ids, give it its own policy and event log from seq 1, and pass the instructions to every agent prompt from `from_stage` on, as a labeled "Analyst instructions" block. Every check still applies. 409 cases as specified. Tests for at least `draft` and `summarize`.

**6. Import the summer archive into S3** (aws-operator for the upload, a builder for the script). Source: `B:\Alioth\Notes\Finance\siemens-healthineers\`, read-only. This is Gordon's data, and the rule about his other code still holds: never copy or run any script from `Notes/Finance/`. Write a new uploader.
- Include primary source files only (`pdf`, `htm`, `html`, `csv`, `json`, `xml`, `txt`) from `01-Annual-Reports`, `02-Quarterly-Reports`, `03-Other-Company-Reports`, `05-Regulatory-and-Legal`, `07-Predecessor-Siemens-AG`, `08-Competitors`, `10-Procurement-Evidence` and `Varian`. About 1,030 files, about 1.1 GB.
- Exclude Gordon's own work: `09-My-Analysis`, `11-Technical-Explainers`, `12-Verification`, `data/`, every `.md` note, `docx`, images and zips. Read `00-Meta/manifest.json`, `_document-index.md` and the `.meta.md` sidecars only to find each file's original URL and collection date.
- S3 layout: `archive/shl/<original relative path>`, plus `archive/shl/manifest.json` (path, original URL or null, sha256, bytes, content type, `collected_at`). Then index each file in the document cache under its original URL (step 7). A file with no known URL keeps its `s3://` URL.
- Record the object count and size in `AWS_RESOURCES.md`.

**7. Document cache and model cache** (one builder, it owns the gateway's fetch and invoke paths).
- Documents: `CACHE_DOCUMENTS=on|off`. The policy decision always comes first. A blocked request never touches the cache. On an allowed fetch, a cache hit returns the stored bytes (`cache: "hit"`, `cached_at`, small latency). A miss fetches live and stores the copy (`cache: "miss"`). Key: the normalized URL. Store body and metadata in S3 under `cache/docs/`. The Guardrails scan still runs on untrusted content, cached or not. `Document.origin` is `archive`, `cache` or `fetched`.
- Models: `CACHE_MODELS=off|record|replay`. `record` stores every live answer. `replay` returns a recorded answer on a hit (tokens as recorded, `cost_usd` 0, `cache: "hit"`) and calls live on a miss. Build the key from the model id, system prompt, user prompt, tool schema and max tokens, after stripping anything that changes between runs (run ids, agent ids, timestamps). Otherwise a rerun never hits.
- `Run.cache_mode` reports what the run actually used: `off`, `documents` or `documents_and_models`.
- Tests: hit, miss, blocked before the cache, kill switch before the cache, scan on cached untrusted pages, replay on a second identical run.

**8. Plan v2.** Add archive sources as `S-011` and up. Keep `S-001` to `S-010` exactly as they are, since the demo depends on S-006, S-009 and S-010. Priorities: SHL annual reports FY2018 to FY2024 for the ROTIC, leverage and liquidity history (today only 3 of 10 years have ROTIC inputs), peer annual reports or 10-Ks from `08-Competitors` for `rotic_vs_peers`, and regulatory or product documents that help `research_stage` if any fit the rubric. Verify every `extract_pages` index against the PDF. Mark archive sources "collected before the event" in their rationale. `check_plan.py` passes.

**9. R2, the full run on the full corpus.** On EC2 with `CACHE_DOCUMENTS=on` and `CACHE_MODELS=record`. Compare it with `run_20261108_pin2ve`: items, pass or fixed, scores, mean certainty, coverage, cost, minutes. Put both in status.md for Gordon. Don't pick the better-looking one silently. If R2 holds up, it becomes the finished run in the demo. Export it as a replay, commit it, `git tag demo-ok-2`.

**10. G6b, the 3-minute demo in the webapp.** With `CACHE_DOCUMENTS=on` and live model calls, tune `DEMO_PACING` so launch to STOP takes at most 55 seconds, with time to revoke S-006 and stop one extractor. Rehearse twice against EC2 with a stopwatch, then rewrite `backend/DEMO.md` for the webapp: what to click, when, and what should appear. Keep the CLI launch line, and add the backup: `CACHE_MODELS=replay` if Bedrock is slow, set with `infra/remote.sh --set-env`.

**11. Hand over.** Put READY FOR INTEGRATION at the top of `backend/status.md`, with the invoke URL, the finished run id, the server env values the demo needs, the frontend env values (`NEXT_PUBLIC_API_BASE`, and the API key if Gordon wants one), and anything cut. Push, and stop starting new work. Tell Gordon to end this session before the integrator starts, so two sessions never deploy to the same instance.

## Parallel work and cutoffs

Do step 1 first and alone. Then 2 to 5 (pipeline and API) can run next to 6 to 8 (archive, cache, plan). Steps 2 and 7 both touch the gateway, so give the gateway to one builder or run them one after the other.

- **2:30 AM:** steps 1 to 8 done.
- **3:15 AM:** R2 done.
- **4:00 AM:** G6b done, READY FOR INTEGRATION.

Gordon approved all four features, so cut only at a cutoff, and tell him first. Cut order: rerun, then chat. Never cut agent stop, the document cache or the timed rehearsal.

## Labels on stage and on Devpost

- The plan is frozen: the needs and sources were prepared before the event.
- Archive documents were collected before the event (summer 2026) and are served from our S3 cache. The pipeline, checks, gateway and cache are new code written during the event.
- The test page is ours and simulated.
- Recorded model answers, if the backup is used.
