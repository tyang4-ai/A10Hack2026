# TLDR for the frontend Claude

You are building the UI for **Axelrod**, a hackathon project (SCU ACM AWS x A10 Hack, Devpost deadline Sun Nov 8, 9:00 AM PT). Gordon builds the backend. You only touch `frontend/`. The API is fixed by `contract/` and `docs/CONTRACT.md`. Build against the mock server now, then switch the base URL to the real API.

## What Axelrod is, in four sentences

An analyst picks a company (the demo uses Siemens Healthineers), Axelrod plans what evidence the BOFFIN framework needs and which sources to pull, and the analyst approves, rejects, reorders or adds sources. Agents then fetch, extract, draft and verify a report. Every request they make goes through one gateway that enforces the approved-source allowlist, logs all traffic live, blocks anything outside policy and stops everything when the analyst hits STOP. The output is a long agent report, where every number is quoted exactly from a stored source and checked by code, plus a 3-page brief where every sentence cites the report, and every item and sentence carries a certainty score.

## Run the mock

```
pip install -r mock/requirements.txt
uvicorn mock.mock_server:app --reload --port 8000     # from the repo root
# Swagger UI: http://localhost:8000/docs
# .env: VITE_API_BASE=http://localhost:8000/api/v1
```

- `run_20261108_fx0001` is a finished run with data in every panel. Start here for layout work.
- `POST /runs` replays the full story live: about 8 s of planning, then it waits at approval until you `POST /approval`, then about 30 s for the rest. `MOCK_SPEED=0.5` runs it at half speed, `MOCK_SPEED=3` at triple speed.
- Stop, revoke, add source, comments, item edits and demo error injection all work.
- The fixture company is fictional ("Northwind Instruments (fixture)", USD). The real demo company reports in EUR. Format from `value.unit`, never hardcode a currency.

## Layout (think Adobe Lightroom panels plus a VS Code bottom drawer)

- **Top bar:** run picker, company and ticker, stage stepper (`Run.stage`), live cost meter (`usage.cost_usd`), counts, a mode badge (`FIXTURE` or `REPLAY` whenever `Run.mode` is not `live`, because simulated things must be labeled), and a big red **STOP** button (`POST /stop`, with a confirm).
- **Left: three modules.** Sources, Agent report, Brief. Switch automatically as the stage advances, and let the analyst click back at any time.
- **Center:** the module's workspace.
- **Right: the inspector.** Whatever is selected: its certainty breakdown, the evidence chain, the checks and the repair history, plus a comment box.
- **Bottom drawer with tabs:** Traffic, Agents, Vault, Log.

## The three modules

**1. Sources** (stages `plan`, `approve`, `collect`)
- Data needs list (`plan.need` events stream in), then source cards grouped by tier (`plan.source`).
- Priority tier: drag to reorder, approve or reject each one. Lower tiers: show them, they auto-approve, and rejecting one is allowed.
- Add a source: URL, kind, optional API key (`POST /sources`, only while awaiting approval). Show the returned card, which never contains the key.
- Coverage warning: compare the in-scope criteria (`contract/boffin_rubric.json` → `depths.screen`) with `Source.criteria` of the sources still approved. "Rejecting S-007 leaves rotic_vs_peers with no source" should show before they commit.
- **Approve & run** sends one `ApprovalRequest` with a decision for every priority source plus `tier` and `rank` for the reordering. A 422 response lists `details.undecided`.
- After approval: status chip per source (fetching, fetched, extracted, failed, skipped, quarantined, revoked), and a **Revoke** button per source (`POST /sources/{id}/revoke`).

**2. Agent report** (stages `extract`, `draft`, `verify`, `repair`)
- Sections by BOFFIN category (`AgentReport.sections`). Rows with a kind badge (D, C, E, G), verification status color and a certainty pill.
- Inspector for an item: citations with the quote, and "open source" opens the document viewer (`GET /documents/{doc_id}/pages/{page}`) with the cited span highlighted (`highlights[]`, where `role: unit_context` gets a lighter style). Show the chain both ways: what it cites and what cites it (build an id map from `report.items`). Show the checks, and the attempt history with diffs.
- Diff rendering for failed `quote_exact` checks: `equal` plain, the agent's wrong characters red with strikethrough, the source's real characters green. The fixture item **D-028** shows this ("Total debt 1,510" vs the real "1,150"). It is a key demo moment.
- Charts: render `Chart` specs yourself (Recharts is fine). Points are already computed. Never recompute them.
- Scorecard from `rollup`: per-category scores, then the weighted average first, both percentages labeled "inference", and coverage "4 of 6 scored". Holes register (`holes[]`) as its own list.

**3. Brief** (stages `summarize`, `verify_summary`)
- A 3-page print layout (print CSS, Letter size, `window.print()` to export).
- **Certainty highlighting, like an AI-detector page:** each sentence gets a background by band (high green, medium amber, low red, none gray). The header shows the report certainty gauge (`certainty.mean`, `high_pct`) **next to** coverage, never merged with it.
- Hover a sentence: tooltip with `certainty.score`, the basis line, and support x inherited x completeness. Click: the inspector walks `derived_from` down through C and D items to the highlighted line in the source page.
- `derived_from` may contain `rollup` (scorecard numbers) or `HOLE-##` ids. Handle both.

## Bottom drawer

- **Traffic:** the A10 judges look here first. A table of `gateway.request` events: time, agent, role, action, target, decision, rule, latency, tokens and cost. `allowed` is neutral, `blocked` red, `flagged` amber. Pin blocked rows or add a filter for them.
- **Agents:** a swarm grid of dots grouped by role, colored by status. It has to stay smooth with 150+ agents. Click a dot to see its task and transcript path.
- **Vault:** the tree from `GET /vault/tree`, with a file preview from `GET /vault/file?path=`.
- **Log:** every event's `message`, with the `agent.log` lines highlighted.

## Data flow

1. On opening a module, fetch its snapshot (`/plan`, `/report`, `/summary`, `/agents`, `/policy`).
2. Poll `GET /runs/{id}/events?after={last_seq}` every 1000 ms. Apply events into one normalized store (sources, items, sentences, agents, traffic). If a page comes back full (`limit` events), poll again right away.
3. On `report.ready` and `summary.ready`, refetch that snapshot for the final rollup, certainty and layout.
4. On `done`, `stopped` or `failed`, poll every 5 s.
5. Ignore unknown event types. Use `isEvent(e, "item.verified")` from `contract/types.ts` to narrow `data`.
6. A reference to an id you don't have yet must render as "missing", never crash.

## Rules

- Import types from `contract/types.ts`. It's generated, so never edit it. Never edit `contract/`, `mock/` or `backend/`. If you need a shape change, ask Gordon and he changes `models.py` first.
- Never call AWS from the browser. All calls go to `VITE_API_BASE`.
- Label simulated things: fixture or replay mode, and the demo error-injection button (`POST /demo/inject-errors`).
- Wording for certainty: "how well this is supported by checked evidence". Never "probability it's true".
- Judges will ask you about your code, so keep it simple enough to explain.

## Build order

**P0, in this order:**
1. App shell, run picker, polling store, stage stepper, STOP button
2. Sources module and the approval flow
3. Traffic drawer and the Revoke buttons
4. Agent report list, inspector, and the document viewer with highlights
5. Brief with certainty highlighting and the report gauge

**P1:** agent swarm, vault browser, charts, diff rendering, scorecard and holes, comments.
**P2:** item edits, the demo injection button, drag-and-drop polish, print tuning.

## Demo moments the UI must make obvious

1. The approval gate (nothing gets fetched until the analyst approves).
2. Live traffic with a red BLOCKED row: a link to an unapproved domain, default deny.
3. Revoke a source mid-run, and the next request to it turns red.
4. STOP: the kill switch banner, plus a blocked model call right after.
5. A flagged page: Guardrails caught a prompt attack and the page is quarantined.
6. Click a brief sentence and follow it down to the highlighted line on the source page.
7. A fixed item showing the diff (D-028).
8. The brief's certainty highlighting and the report gauge next to coverage.
