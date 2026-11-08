# Backend v2: Bobby, agent controls, document cache, 3-minute demo

For the backend orchestrator. Gordon approved all of this on Sat Nov 7 between 11 PM and midnight. Start with `git fetch origin && git merge origin/main` (contract v0.3.0), then read `docs/CONTRACT.md` sections 13 and 14 and `contract/CHANGELOG.md`. The mock answers every new route, so check behavior there first. Turn the work below into plans in `backend/PLANS.md` with file ownership, as before. Your runbook rules still hold.

## What Gordon decided

1. **Bobby runs the show.** Every run has one orchestrator agent, Bobby (`agent_id: "bobby"`, role `orchestrator`, Sonnet). He makes the big calls at the stage gates, sends goons back to rework, and answers comments. Every other agent is one of "Bobby's goons" (`name: "Bobby's goon #<n>"`). Code keeps him in bounds (CONTRACT.md section 14).
2. **Tong's requests are approved:** stop one agent, agent chat, resume after STOP, rerun from a step, and rerun one agent with instructions. Reworks work on single goons as well as whole steps.
3. **Match the webapp.** Every feature in Tong's UI must work against the real backend. The one missing today: item and sentence comments never resolve. Bobby resolves them now (step 5).
4. **`source_revoked` is approved** (`item.updated` reason). Switch the revoke cascade over to it.
5. **The demo runs on cached documents.** Gordon's summer 2026 archive of SHL source documents goes into S3, plus copies of everything Axelrod fetched tonight. The gateway still decides every request, then serves the body from the cache. Everything cached is labeled (`Run.cache_mode`, `gateway.request.cache`, `Document.origin`).
6. **The demo is 3 minutes, webapp first.** The CLI is on screen for about 10 seconds. Script: `docs/PLAN.md`, "Demo script: 3 minutes".
7. **When you finish, a second Claude Code session integrates and deploys** (`docs/INTEGRATE.md`). You hand over with READY FOR INTEGRATION (step 13).
8. **Credentials are logged.** Any credential you create or use (an API key, a token, a key pair) gets a line in `backend/AWS_RESOURCES.md` under "Credentials": name, where it lives, how to revoke it. Never the value. Gordon deletes everything right after the hackathon.

## Work, in order

**1. Wire the v0.2 and v0.3 shapes.**
- Run: `parent_run_id`, `rerun_from`, `instructions`, `cache_mode`.
- Agent: `name`, `parent_agent_id`, `instructions`, `rerun_by`. Every agent gets a name: "Bobby", or "Bobby's goon #<n>" in spawn order.
- Document: `origin` and `collected_at`.
- GatewayRequestData: `cache` and `cached_at`.
- PolicyRule: `agent_id`, and origin `user_agent_stop`.
- AgentStatusData: `reason`.
- Role `orchestrator` with a large-model role scope.
- Events `agent.message`, `agent.decision` and `run.resumed`.
- Item reasons `source_revoked` and `rework`, and repair action `rework`.

Then `contract_smoke.py` against the real API must still pass, and should cover the new routes.

**2. Stop one agent.** `POST /runs/{id}/agents/{agent_id}/stop`. In `decide()`, right after the kill switch: a request from a stopped agent is denied by `R-stop-<agent_id>`. A result that lands after the stop is discarded, as with STOP. The run keeps going. The work that agent owned becomes a declared hole, "agent stopped by the analyst", never silently missing. Stopping Bobby means `proceed` at every later gate. Tests.

**3. Rerun one agent.** `POST /runs/{id}/agents/{agent_id}/rerun`, as in CONTRACT.md section 14. New agent `<root>.<k>`, same role and targets, the instructions as a labeled "Instructions" block in its prompt, under the current policy. Its outputs become new versions of the same item ids (`item.updated`, reason `rework`, the old version kept as an attempt). With `recheck`, re-check everything citing the changed items, and reopen a finished run, then complete it again. This one machine serves the analyst (`rerun_by: "analyst"`) and Bobby (`"orchestrator"`). Tests, including a rerun of a rerun.

**4. Bobby at the gates.** A new agent module. Bobby spawns first and stays `working` until the run ends. After `plan`, `extract`, `repair` and `verify_summary`, code builds a short digest: counts, failures by check, holes, budget left. Bobby answers with a forced tool call: `proceed`, `rework` (at most 3 goons per gate, each once, each with instructions) or `escalate` (a note for the analyst). Code validates the call (the agents exist and are goons, the budget allows it, no repeats), runs the reworks through step 3, and emits `agent.decision`. He never touches sources, policy or check results, and every call goes through the gateway. Keep his prompts short, so a gate costs cents and a few seconds. Tests with fake models: proceed, a valid rework, an invalid rework rejected by code, a stopped Bobby.

**5. Comments go to Bobby.** `POST /comments` already stores the comment. Now Bobby reads it with the target item or sentence and its citations. He either reruns the goon that produced it with the comment as instructions (step 3, with recheck), or answers without a change. Then `comment.resolved` with his answer in `response`, and an `agent.decision` with `comment_id`. A stopped run still rejects comments.

**6. Document cache.** `CACHE_DOCUMENTS=on|off`. The policy decision always comes first. A blocked request never touches the cache. On an allowed fetch, a cache hit returns the stored bytes (`cache: "hit"`, `cached_at`, small latency). A miss fetches live and stores a copy (`cache: "miss"`). Key: the normalized URL. Store the body and metadata in S3 under `cache/docs/`. The Guardrails scan still runs on untrusted content, cached or not. `Document.origin` is `archive`, `cache` or `fetched`. `Run.cache_mode` reports what the run used. Tests: hit, miss, blocked before the cache, kill switch before the cache, scan on a cached untrusted page.

**7. Import the summer archive into S3** (aws-operator for the upload, a builder for the script). Source: `B:\Alioth\Notes\Finance\siemens-healthineers\`, read-only. This is Gordon's data, and the rule about his other code still holds: never copy or run any script from `Notes/Finance/`. Write a new uploader.
- Include primary source files only (`pdf`, `htm`, `html`, `csv`, `json`, `xml`, `txt`) from `01-Annual-Reports`, `02-Quarterly-Reports`, `03-Other-Company-Reports`, `05-Regulatory-and-Legal`, `07-Predecessor-Siemens-AG`, `08-Competitors`, `10-Procurement-Evidence` and `Varian`. About 1,030 files, about 1.1 GB.
- Exclude Gordon's own work: `09-My-Analysis`, `11-Technical-Explainers`, `12-Verification`, `data/`, every `.md` note, `docx`, images and zips. Read `00-Meta/manifest.json`, `_document-index.md` and the `.meta.md` sidecars only to find each file's original URL and collection date.
- S3 layout: `archive/shl/<original relative path>` plus `archive/shl/manifest.json` (path, original URL or null, sha256, bytes, content type, `collected_at`). Index each file in the document cache under its original URL. A file with no known URL keeps its `s3://` URL.
- Record the object count and size in `AWS_RESOURCES.md`.

**8. Plan v2.** Add archive sources as `S-011` and up. Keep `S-001` to `S-010` exactly as they are, since the demo depends on S-006, S-009 and S-010. Priorities:
- SHL annual reports FY2018 to FY2024 (`01-Annual-Reports`) for the ROTIC, leverage and liquidity history. Today only 3 of 10 years have ROTIC inputs.
- Peer annual reports or 10-Ks from `08-Competitors` for `rotic_vs_peers`.
- Regulatory or product documents that help `research_stage`, if any fit the rubric.

Verify every `extract_pages` index against the PDF. Mark archive sources "collected before the event" in their rationale. `check_plan.py` passes.

**9. Resume.** `POST /runs/{id}/resume`, only from `stopped`. Kill switch off, policy version bump, `run.resumed` then `run.status` running. Restart the stage STOP cut into and skip work that already finished. Agents stopped one by one stay stopped. Tests.

**10. Agent chat.** `GET` and `POST /runs/{id}/agents/{agent_id}/messages`, ids `AM-001`. The reply comes from that agent's role and model, through the gateway, with its task, inputs and transcript as context. Bobby answers about his calls. Chat is read-only and labeled not checked. Keep replies to about 120 words.

**11. Rerun from a step and the model cache.** `POST /runs/{id}/rerun` forks a run as in CONTRACT.md section 13: fork the state up to `from_stage`, keep item ids, give the fork its own policy and event log, and pass the instructions to every agent from `from_stage` on. Model cache: `CACHE_MODELS=off|record|replay`, keyed on the model id, prompts, tool schema and max tokens, after stripping run ids, agent ids and timestamps. On a hit, tokens are as recorded, `cost_usd` is 0 and `cache` is `hit`.

**12. R2, then the timed rehearsal.**
- **R2:** the full run on the full corpus. On EC2 with `CACHE_DOCUMENTS=on` (and `CACHE_MODELS=record` if step 11 landed). Compare it with `run_20261108_pin2ve`: items, pass or fixed, scores, mean certainty, coverage, cost, minutes, Bobby's calls. Put both in status.md for Gordon, and don't pick silently. If R2 holds up, export it as a replay, commit it, and `git tag demo-ok-2`.
- **G6b:** the 3-minute demo in the webapp. With cached documents and live model calls, tune `DEMO_PACING` so launch to STOP takes at most 55 seconds, with time to revoke S-006 and stop one goon. Rehearse twice against EC2 with a stopwatch, then rewrite `backend/DEMO.md` for the webapp: what to click, when, and what should appear.

**13. Hand over.** Put READY FOR INTEGRATION at the top of `backend/status.md`. Include:
- the invoke URL and the finished run id
- the server env values the demo needs
- the frontend env values (`NEXT_PUBLIC_API_BASE`, and the API key if Gordon wants one)
- anything you cut

Push, and stop starting new work. Tell Gordon to end this session before the integrator starts, so two sessions never deploy to the same instance.

## Parallel work and cutoffs

Do step 1 first and alone. The gateway is touched by steps 2, 4 (Bobby's role scope) and 6, so give it to one builder or sequence them. Steps 3 to 5 share the rework machine, so one builder, or two in sequence. Steps 7 and 8 can run next to all of that.

- **1:45 AM:** steps 1 to 6 done.
- **2:30 AM:** steps 7 to 10 done.
- **3:15 AM:** R2 done.
- **4:00 AM:** G6b done, READY FOR INTEGRATION.

Gordon approved everything, so cut only at a cutoff, and tell him first. Cut order: step 11, then chat, then resume. Never cut Bobby, rerun one agent, stop one agent, the document cache, the archive and plan v2, or the timed rehearsal.

## Labels on stage and on Devpost

- The plan is frozen: the needs and sources were prepared before the event.
- Archive documents were collected before the event (summer 2026) and are served from our S3 cache. The pipeline, Bobby, the checks, the gateway and the cache are new code written during the event.
- The test page is ours and simulated.
- Recorded model answers, if that backup is used.
