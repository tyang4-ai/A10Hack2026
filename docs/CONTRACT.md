# Axelrod API contract (v0.3.0)

The source of truth is `contract/models.py`. This file explains it. `contract/types.ts` is generated from the models, and the mock server validates every response against them. If this doc and `models.py` ever disagree, `models.py` wins. Fix the doc.

Priorities: **P0** = the demo needs it, **P1** = should have, **P2** = stretch.

## 1. Basics

| Thing | Rule |
|---|---|
| Base URL | `NEXT_PUBLIC_API_BASE` in the frontend (Next.js) = `http://localhost:8000/api/v1` (mock), later the API Gateway invoke URL + `/api/v1`. Paths below are relative to it |
| Health | `GET /healthz` (outside `/api/v1`) returns `Health` |
| Auth | None for the hackathon. If `AXELROD_API_KEY` is set on the backend, send it as header `X-Axelrod-Key` |
| CORS | Backend allows any origin |
| JSON | snake_case keys. Every response field is always present. Optional fields are `null`, never missing |
| Time | ISO 8601 UTC with `Z`, e.g. `2026-11-08T03:00:00.000Z` |
| Money | `cost_usd` is AWS spend in USD. Report values carry their own unit (Siemens Healthineers reports in EUR) |
| Errors | Always `{"error": {"code", "message", "details"}}` with HTTP 404 `not_found`, 409 `invalid_state` or `conflict`, 422 `validation_error`, 500 `internal` |

IDs:

| Prefix | Thing | Example |
|---|---|---|
| `run_` | run | `run_20261108_fx0001` |
| `N-` | data need | `N-03` |
| `S-` | source | `S-002` |
| `DOC-` | stored document | `DOC-001` |
| `D-` | data item | `D-028` |
| `C-` | conclusion | `C-001` |
| `E-` | extrapolation (one BOFFIN criterion score) | `E-001` |
| `G-` | chart | `G-001` |
| `H-` | brief sentence | `H-005` |
| `HOLE-` | declared gap | `HOLE-03` |
| `R-` | policy rule | `R-revoke-S-006` |
| `REQ-` | gateway request | `REQ-0042` |
| `CM-` | comment | `CM-001` |

A chart point can reference a computation inside a conclusion as `C-001:rotic_fy2025`.

## 2. Run lifecycle

```
status:  running ──────────────> awaiting_approval ──(POST /approval)──> running ──────────────────────────> done
stage:   plan                     approve                                 collect → extract → draft → verify
                                                                          → repair → summarize → verify_summary → complete
any time: POST /stop → status stopped (kill switch). failed = backend error, see Run.error
```

| Stage | Panel | What happens |
|---|---|---|
| `plan` | Sources | Planner maps BOFFIN criteria to data needs, then proposes and ranks sources |
| `approve` | Sources | Waits for the analyst. Priority sources need an explicit decision |
| `collect` | Sources | Fetchers pull approved sources through the gateway. Untrusted pages get a Guardrails scan |
| `extract` | Agent report | One extractor per page or chunk pulls data items with verbatim quotes. Code checks every quote right away |
| `draft` | Agent report | Drafter writes conclusions, scores and chart specs from verified data only |
| `verify` | Agent report | Code checks numbers. Three isolated judges check each claim |
| `repair` | Agent report | Failed items get weakened toward their evidence, at most twice, then become holes |
| `summarize` | Brief | The 3-page brief, every sentence cited to report items |
| `verify_summary` | Brief | Same checks on every brief sentence |
| `complete` | all | Done. Certainty and rollup are final |

`Run.mode`: `live`, `replay` (a recorded real run played back) or `fixture` (mock data). The UI must show a visible label when the mode is not `live`.

## 3. Endpoints

All paths below are relative to `NEXT_PUBLIC_API_BASE`.

| P | Method and path | Body | Returns | Notes |
|---|---|---|---|---|
| P0 | `POST /runs` | `CreateRunRequest` | `Run` (201) | Starts planning |
| P0 | `GET /runs` | | `RunList` | Newest first. The mock includes a finished fixture run |
| P0 | `GET /runs/{run_id}` | | `Run` | Status, stage, counts, usage, `last_seq` |
| P0 | `GET /runs/{run_id}/events?after={seq}&limit={n}` | | `EventsPage` | Poll this. See section 4 |
| P0 | `GET /runs/{run_id}/plan` | | `Plan` | Needs and sources, sorted by tier then rank |
| P0 | `POST /runs/{run_id}/approval` | `ApprovalRequest` | `ApprovalResponse` | 409 unless `awaiting_approval`. 422 lists `details.undecided` or `details.unknown` |
| P0 | `POST /runs/{run_id}/stop` | `StopRequest` | `Run` | Kill switch. 409 if already finished |
| P0 | `POST /runs/{run_id}/sources/{source_id}/revoke` | `RevokeRequest` | `RevokeResponse` | After approval only. The next request to that source is blocked |
| P0 | `GET /runs/{run_id}/policy` | | `Policy` | What the gateway enforces right now |
| P0 | `GET /runs/{run_id}/report` | | `AgentReport` | Partial while drafting. `rollup`, `certainty` and `holes` appear when `status` is `final` |
| P0 | `GET /runs/{run_id}/summary` | | `HumanReport` | `not_started` until the summarize stage |
| P0 | `GET /runs/{run_id}/documents/{doc_id}/pages/{page}` | | `DocumentPage` | Page text plus highlight spans for the citation viewer |
| P1 | `POST /runs/{run_id}/sources` | `AddSourceRequest` | `Source` (201) | Only while awaiting approval. `api_key` is write-only and is never echoed |
| P1 | `GET /runs/{run_id}/documents` | | `DocumentList` | Fetched documents |
| P1 | `GET /runs/{run_id}/documents/{doc_id}` | | `Document` | Metadata, scan result, vault paths |
| P1 | `GET /runs/{run_id}/agents` | | `AgentList` | Every agent ever spawned in the run |
| P1 | `GET /runs/{run_id}/vault/tree` | | `VaultNode` | Folder tree for the vault drawer |
| P1 | `GET /runs/{run_id}/vault/file?path={path}` | | `VaultFile` | Text up to 1 MB, otherwise `download_url` |
| P1 | `POST /runs/{run_id}/comments` | `CommentRequest` | `Comment` (201) | Item comments trigger a revision and a re-check |
| P1 | `GET /runs/{run_id}/comments` | | `CommentList` | |
| P2 | `PATCH /runs/{run_id}/items/{item_id}` | `ItemPatch` | `Item` | Analyst edit. The backend re-verifies it. Charts cannot be edited |
| P2 | `POST /runs/{run_id}/demo/inject-errors` | `InjectErrorsRequest` | `InjectErrorsResponse` | Demo control. Corrupts one digit in N quotes so the checker can catch them. Label it in the UI |
| P1 | `POST /runs/{run_id}/agents/{agent_id}/stop` | `StopRequest` | `Agent` | v0.2. Stops one agent, the run keeps going. See section 13 |
| P1 | `GET /runs/{run_id}/agents/{agent_id}/messages` | | `AgentMessageList` | v0.2. Chat with one agent, oldest first |
| P1 | `POST /runs/{run_id}/agents/{agent_id}/messages` | `AgentMessageRequest` | `AgentMessage` (201) | v0.2. The analyst's message. The reply arrives as an `agent.message` event |
| P1 | `POST /runs/{run_id}/resume` | `ResumeRequest` | `Run` | v0.2. Turns the kill switch off and picks up where STOP cut in |
| P1 | `POST /runs/{run_id}/rerun` | `RerunRequest` | `Run` (201) | v0.2. Forks a new run from a step, with instructions. The parent never changes |
| P1 | `POST /runs/{run_id}/agents/{agent_id}/rerun` | `AgentRerunRequest` | `Agent` (201) | v0.3. Reruns one goon with instructions, in the same run. See section 14 |

A stopped run rejects comments, edits, injections and agent chat (409, or a `rejected` comment) until it resumes.

## 4. Live updates: snapshot plus events

1. Open a panel: fetch its snapshot (`/plan`, `/report`, `/summary`, `/agents`, `/policy`). Each snapshot has `as_of_seq`.
2. Poll `GET /events?after={last_seq}` every 1000 ms. Apply events in `seq` order. Store `last_seq` from the response.
3. When `run_status` is `done`, `stopped` or `failed`, slow polling to every 5 s.
4. Ignore event types you don't know. The backend may add new ones.
5. `limit` defaults to 500 (max 2000). If you get `limit` events back, poll again right away.

Every event has `seq`, `ts`, `run_id`, `type`, `stage`, `agent_id`, `level` and `message` (one readable line for log views), plus a typed `data`. In TypeScript, `isEvent(e, "gateway.request")` narrows `e.data`.

| `type` | `data` | Use it for |
|---|---|---|
| `run.created` | `{run}` | Run header |
| `run.status` | `{status, stage}` | Stepper, panel switching |
| `run.stopped` | `{reason}` | Kill switch banner |
| `run.failed` | `{error}` | Error banner |
| `run.completed` | `{}` | Done state |
| `run.resumed` | `{reason, stage}` | v0.2. Kill switch off, the run picks up at `stage` |
| `plan.need` | `{need}` | Needs list, streams in |
| `plan.source` | `{source}` | Source cards, streams in (also fires when the analyst adds one) |
| `plan.ready` | `{needs, sources}` | Enable the approval controls |
| `approval.committed` | `{approved, rejected, auto_approved}` | Lock the approval UI |
| `policy.updated` | `{version, change, rule_ids}` | Policy view, traffic annotations |
| `source.status` | `{source_id, status, stats, error}` | Source chips |
| `doc.stored` | `{doc_id, source_id, pages, vault_path, scan}` | Vault, quarantine badge |
| `agent.spawned` | `{agent}` | Agent swarm |
| `agent.status` | `{agent_id, status, task, usage, reason}` | Agent swarm. `reason` is set when the analyst stopped the agent |
| `agent.log` | `{text, transcript_path}` | Thought log |
| `agent.message` | `{message}` | v0.2. Agent chat. The same `message_id` can arrive again with a new `status`: replace it |
| `agent.decision` | `{agent_id, stage, decision, summary, reworks, comment_id}` | v0.3. One of Bobby's calls: `proceed`, `rework` or `escalate` |
| `gateway.request` | `GatewayRequestData` | Traffic table. `decision` is `allowed`, `blocked` or `flagged`. v0.2: `cache` is `hit`, `miss` or null |
| `item.created` | `{item}` | New report row (status `pending`) |
| `item.verified` | `{item_id, verification, certainty}` | Row color and certainty pill |
| `item.updated` | `{item, reason}` | Replace the row. `reason`: `repair`, `comment`, `user_edit`, `demo_injection`, `source_revoked` (v0.2), `rework` (v0.3) |
| `report.ready` | `{items, passed, fixed, failed, holes}` | Refetch `/report` for rollup and certainty |
| `summary.sentence` | `{sentence, section_heading}` | Brief draft streaming in |
| `summary.verified` | `{sentence_id, verification, certainty}` | Sentence highlight |
| `summary.ready` | `{sentences, words}` | Refetch `/summary` for the final layout |
| `comment.created` | `{comment}` | Comment thread |
| `comment.resolved` | `{comment}` | Comment thread |
| `usage.updated` | `{usage}` | Cost meter |

## 5. Plan and approval

- `Source.tier` is `priority`, `tier_2` or `tier_3` (Tong's draft called these 1, 2 and 3). `rank` is the order inside a tier (1 = fetched first).
- Tiers come from a domain list kept in code: primary (filings, annual reports, IR, regulators, exchanges), reputable secondary (wire services, financial press, trade press), low trust (forums, blogs). The planner can propose a lower tier, never a higher one. Only the analyst can promote a source, by moving it at approval.
- Tier rules, checked by code as `tier_rule`: a tier_3 source is never the only support for a number, and a tier_2 number must not contradict a tier 1 figure for the same metric and period.
- `Source.scores.reliability` (0 to 1) feeds the certainty score of every data item from that source.
- `Source.criteria` lists the BOFFIN criteria the source serves. The frontend can warn "if you reject this, rotic_vs_peers has no source left" by comparing the in-scope criteria (`contract/boffin_rubric.json` → `depths.screen`) against the criteria of sources that are still approved.
- `ApprovalRequest.decisions` must include **every priority source** (approve or reject). Send `tier` and `rank` to record a drag reorder or a tier move. Non-priority sources you leave out become `auto_approved`. Analyst-added sources are approved unless you reject them.
- Approval builds the gateway policy: an allow rule per approved source (its domain and path prefix, fetcher role only), a deny rule per rejected source, default deny for everything else.

## 6. Gateway policy

The gateway is the only path from an agent to the outside world. Agents never hold an HTTP client or an AWS client. Each request is decided in this order, and the first match wins:

1. Kill switch on (run stopped): deny.
2. Budget exceeded: deny.
3. Role scope: the agent's role must allow the action (and the model, for `model.invoke`).
4. Explicit deny rules (revoked or rejected sources): deny.
5. Allow rules (domain + path prefix): allow.
6. Default: deny.

v0.2 adds one check right after the kill switch: a stopped agent (`R-stop-<agent_id>`, a deny rule with `agent_id` set) gets every request denied, while the rest of the run continues.

Any error inside the gateway also denies (fail closed). Every decision emits `gateway.request` with latency, and with tokens and cost for model calls. `flagged` means the fetch was allowed but the Bedrock Guardrails scan found a prompt attack in the content: the page is stored in the vault and quarantined, and no agent ever reads it.

## 7. Agent report items

Four kinds share `id`, `kind`, `category`, `verification` and `certainty`:

| Kind | Key fields | Built by |
|---|---|---|
| `data` (D) | `statement`, `value`, `citations[]`, `source_id`, `source_tier`, `extracted_by` (`code` or `agent`) | Extractors (text) or code (structured data) |
| `conclusion` (C) | `text`, `data_ids[]`, `computations[]` | Drafter |
| `extrapolation` (E) | `criterion`, `score` (-1, 0, 1 or null = N/A), `na_reason`, `conclusion_ids[]`, `rule`, `confidence` | Drafter, rules from the rubric |
| `chart` (G) | `chart_type`, `unit`, `scale`, `series[].points[]` with `ref`, `refs[]` | Code only. Never drawn by a model |

**Citations.** `locator.type` is `text` (`page`, `char_start`, `char_end` into the stored page text, end exclusive. `page` is the 1-based page index in the stored file, not the printed page label) or `json` (`pointer`, an RFC 6901 JSON pointer into a stored JSON document). `quote` is the exact text at that span. `role` is `evidence`, `unit_context` (for example the "(in millions of €)" header of a table) or `period_context`.

**Values.** `value.number` is as reported before scale. `value.as_reported` is the exact characters in the quote (`"1,150"`, `"(1,204)"`). `scale` is `one`, `thousand`, `million` or `billion`. Display with `unit` and `scale`, never with a hardcoded currency.

**Computations.** `expr` uses `+ - * /`, parentheses, numbers, data ids (their base-unit values, with percentages as fractions) and the names of earlier computations in the same conclusion. `display` is how the result appears in the text, rounded half up.

**Verification.** `status`: `pending`, `pass`, `fixed` (failed, then passed after repair), `fail` (still failing after repair, now a hole) or `blocked` (the check could not run, which never counts as a pass). `checks[]` holds the latest results. `attempts[]` holds history, each attempt with its own `checks`, the repair `action` (`recite`, `recompute`, `downgrade`, `delete`, `not_determinable`, `reword`, `user_edit`) and the `before` and `after` text. A failed `quote_exact` check carries `diff[]` segments (`op` = `equal`, `insert`, `delete`, `replace`, with the agent's characters and the source's characters) and `closest_locator`, the real span. Judge checks carry `votes_for` and `votes_total` and may list `unsupported_spans`.

**Rollup** (when final): one `CategoryScore` per in-scope category, `weighted_average` (lead with this), `normalized_pct` and `percent_of_max_pct` (both are inferences about the original scoresheet, so show both or neither), coverage `scored / in_scope` and the N/A criteria by name.

**Holes.** Every gap is declared: unscored criteria, failed items, failed fetches. Show them as their own register. Never hide one.

## 8. Certainty

Every item and every brief sentence carries `certainty`. Both reports carry a `ReportCertainty`. All of it is computed by code from verification results (reference: `contract/certainty.py`). No model is ever asked how confident it is.

```
strength  = support x inherited
certainty = strength x completeness            shown as an integer 0 to 100

support      1.0 when code proves it (exact quote, recomputed numbers, chart from refs),
             judge agreement (votes_for / votes_total) for semantic checks, 0 if failed
inherited    data items: the source's reliability. Everything else: the weakest strength among its inputs
completeness BOFFIN scores only: confidence high 1.0, medium 0.85, low 0.6. Not inherited
```

Bands: **high** 85 and up, **medium** 60 to 84, **low** under 60, **none** for failed, blocked, pending or unscored. `weakest_ref` names the input that holds the score down, and `basis` is a one-line explanation for the tooltip.

`ReportCertainty`: `mean` (word-weighted for the brief, plain for the agent report), `high_pct` (share of words, or of items, in the high band), counts per band, and coverage (`coverage_scored` of `coverage_in_scope` criteria). Always show coverage next to certainty. Never merge them into one number.

Wording for the UI: "How well this is supported by checked evidence". Not "probability it's true". A data item can be quoted perfectly from a weak source, and certainty shows that through the source's reliability.

## 9. Brief (human report)

`sections[].blocks[]`, where a block is `paragraph` or `bullets` (sentences), `chart` (`chart_id` plus a `caption` sentence) or `scorecard` (render `AgentReport.rollup` and the E items). Every `SummarySentence` has `mode` (`copied` = identical to one item's text, `derived` = summarized) and `derived_from[]`: item ids, `rollup` for scorecard numbers, or `HOLE-##` ids. The brief must fit 3 printed pages. `stats.est_pages` is an estimate, so check it in print preview.

## 10. Documents and the vault

`DocumentPage.text` is exactly the text the checker matched against, and `highlights[]` are the spans cited on that page (`item_id`, offsets, `status`, `role`). For `content_type: "json"` the text is pretty-printed JSON, and citations use pointers instead of highlights. `Document.paginated` is `true` for real PDF pages and `false` when the backend split HTML into pseudo-pages. `Document.quarantined` with `scan.verdict: "flagged"` means the page held a prompt attack.

The vault mirrors the per-company layout of Gordon's finance vault:

```
runs/<run_id>/<company-slug>/
  00-Meta/              _plan.json  _url-manifest.json  _document-index.json  _status.json
                        events.jsonl  agents/<agent_id>.jsonl (full prompts and outputs)
  01-Annual-Reports/    <file>.pdf  <file>.md (extracted text with page markers)  <file>.pdf.meta.json
  02-Quarterly-Reports/   03-Other-Company-Reports/   04-News-Coverage/
  05-Regulatory-and-Legal/   06-Macro-Political-Social/   07-Ecosystem-Crosslinks/
  08-Competitors/       09-Analysis/ (agent-report.json/.md, human-report.json/.md)
  10-Demand-Evidence/   11-Technical-Explainers/   12-Verification/ (data-checks.json, reasoning-checks.json)
```

## 11. Number formatting

- Format from `value.unit` and `value.scale`. The demo company reports in EUR millions.
- Round half up (`5.85` → `5.9`). JavaScript's `toFixed` and Python's `format` both round 5.85 to 5.8 because of float error.
- Negative numbers in filings often appear as `(1,204)`. `as_reported` keeps the original characters.

## 12. Changing this contract

Edit `contract/models.py`, run `python contract/gen_types.py`, update the mock and the fixtures in the same commit (`python mock/build_fixtures.py` must still pass), add a line to `contract/CHANGELOG.md`, bump `contract/VERSION`, and tell the other person.

## 13. v0.2 additions: agent control, rerun, resume, demo cache

**Stop one agent.** `POST /runs/{run_id}/agents/{agent_id}/stop` with `{"reason"}` returns the `Agent` with `status: "stopped"`. Effects, in order: `agent.status` (stopped, with `reason`), a deny rule `R-stop-<agent_id>` (`agent_id` set, `origin: "user_agent_stop"`, action = the agent's action) with `policy.updated`, and the agent's next request shows as `blocked` in traffic. A result that lands after the stop is discarded. The work that agent owned becomes a declared hole ("agent stopped by the analyst"), never silently missing. 409 if the agent already finished or the run is done, stopped or failed. 404 for an unknown agent.

**Agent chat.** `POST .../agents/{agent_id}/messages` with `{"text"}` returns the analyst's `AgentMessage` (`status: "sent"`). The agent answers from its own task, inputs and transcript, with its own model and through the gateway, so the kill switch, a stop rule and the budget all apply. Two `agent.message` events follow: the reply (`author: "agent"`, `reply_to` set) and the analyst's message again with `status: "answered"` (or `failed`). Chat is read-only: it never changes the run, the report or the brief, and replies are not checked. Label them in the UI. 409 while the run is stopped. Talking to a finished agent is fine.

**Resume.** `POST /runs/{run_id}/resume` works only on a `stopped` run. It turns the kill switch off (policy version bump, `policy.updated`), emits `run.resumed` and `run.status` (`running`), and restarts the stage STOP cut into, skipping work that already finished. Agents the analyst stopped one by one stay stopped. 409 for any other status.

**Rerun from a step.** `POST /runs/{run_id}/rerun` with `{"from_stage", "instructions"}` creates a new run (201) and never changes the parent. Allowed when the parent is `done`, `stopped` or `failed` and reached `from_stage`. 409 for `complete`, for a step the parent never reached, or while the parent is running. The new run has `parent_run_id`, `rerun_from` and `instructions`, its own policy (the parent's rules at that point) and its own event log from seq 1 (its `run.created` carries the parent fields). Copied items keep their ids, so citations still resolve. The instructions go to every agent from `from_stage` on, and every check still applies to what they produce.

| `from_stage` | Kept from the parent | Redone |
|---|---|---|
| `plan` | nothing | everything, instructions to the planner |
| `approve` | needs and proposed sources | the approval gate (`awaiting_approval`) |
| `collect` | plan and approvals | fetching |
| `extract` | stored documents | extraction onward |
| `draft` | verified data items | conclusions and scores onward |
| `verify` | the draft | checks onward |
| `repair` | checked items | repair onward |
| `summarize` | the verified agent report | the brief |
| `verify_summary` | the brief | brief checks |

**Demo cache.** For a fast, repeatable demo the backend can serve fetch bodies from an S3 document cache, and replay recorded model answers. Everything stays labeled:
- `Run.cache_mode`: `off`, `documents` (fetch bodies from the cache, model calls live) or `documents_and_models` (model answers replayed from recorded calls too). Show a badge for anything but `off`.
- `gateway.request.cache`: `hit` (served from the cache, no network or model call made, `cost_usd` 0), `miss` (done live and stored) or null (cache off). `cached_at` says when the cached copy was made. The policy decision always comes first: a blocked request never reads the cache.
- `Document.origin`: `fetched` (this run), `cache` (an earlier Axelrod run) or `archive` (collected before the event, Gordon's summer 2026 archive). `collected_at` is when the bytes were first collected.

## 14. v0.3 additions: Bobby and his goons, rerun one agent

**Bobby.** Every run has one orchestrator agent: `agent_id: "bobby"`, `role: "orchestrator"`, `name: "Bobby"`, on the large model. Bobby makes the big calls and never does the legwork. Every other agent is one of Bobby's goons, with `name: "Bobby's goon #<n>"` in spawn order, on the large or small model as before. The UI shows Bobby first and groups everyone else under "Bobby's goons".

Bobby decides at fixed points and logs each call as an `agent.decision` event:
- **Stage gates:** after `plan`, `extract`, `repair` (the report is final) and `verify_summary`. Code hands him a digest of the stage (counts, failures by check, holes, budget). He answers `proceed`, `rework` (at most 3 goons per gate, each once, listed in `reworks`) or `escalate` (a note for the analyst, and the run carries on).
- **Comments:** when the analyst comments on an item or a sentence, Bobby reads it and either sends the goon that produced it back with the comment as instructions, or answers without a change. Either way the comment resolves with his answer (`comment.resolved`, `response`), and the decision event carries `comment_id`.
- **Chat:** the analyst can talk to Bobby like any agent (section 13).

Code keeps him in bounds. Bobby's calls go through the gateway like any agent's (role scope, budget, kill switch). He can't approve, reject or revoke sources, change the policy, mark an item as passed, or spend past the budget. Only code checks decide pass or fail. If Bobby is stopped, the run carries on with `proceed` at every gate and logs that his calls are off.

**Rerun one agent.** `POST /runs/{run_id}/agents/{agent_id}/rerun` with `{"instructions", "recheck"}` reruns one goon in the same run, unlike rerun-from-a-step, which forks. The analyst and Bobby both use it (`rerun_by`).
- The new agent has the same role and targets, `agent_id` `<root>.<k>` (`extractor-03.2`, then `extractor-03.3`), `parent_agent_id` set to the agent it reran, and the `instructions`. It emits `agent.spawned` and goes through the gateway under the current policy, so an instruction that asks for an unapproved source is blocked like any other request.
- Its outputs become new versions of the same item ids (`item.updated` with reason `rework`), so citations keep resolving. The previous version stays in `verification.attempts` as an attempt with `action: "rework"`.
- With `recheck: true`, everything that cites a changed item (conclusions, scores, charts, brief sentences) is re-checked and its certainty recomputed (`item.verified`, `summary.verified`). A finished run goes back to `running` for that and completes again. With `recheck: false`, only the agent's own outputs change.
- Errors: 404 for an unknown agent. 409 if the agent is still working (stop it first), if the run is stopped, or for Bobby (talk to him instead).

