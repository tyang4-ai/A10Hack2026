# TLDR for the frontend Claude

You are building the dashboard for **Axelrod**, a hackathon project (SCU ACM AWS x A10 Hack, Devpost deadline Sun Nov 8, 9:00 AM PT). You work on the `frontend` branch, in `frontend/` only. Gordon builds the backend on the `backend` branch. The shared contract lives on `main` in `contract/` and `docs/CONTRACT.md`. Pick up changes with `git fetch origin && git merge origin/main`.

Tong's scaffold (Vite + React + TS, with the API behind one `Api` interface in `src/api/`) was built from an earlier draft, `docs/API.md`, before the contract existed. So the first job is moving `src/api/` onto the contract (section below). Then build against the mock server, and later switch `VITE_API_URL` to the real API.

## What Axelrod is, in four sentences

An analyst picks a company (the demo uses Siemens Healthineers), Axelrod plans what evidence the BOFFIN framework needs and which sources to pull, and the analyst approves, rejects, reorders or adds sources. Agents then fetch, extract, draft and verify a report. Every request they make goes through one gateway that enforces the approved-source allowlist, logs all traffic live, blocks anything outside policy and stops everything when the analyst hits STOP. The output is a long agent report, where every number is quoted exactly from a stored source and checked by code, plus a 3-page brief where every sentence cites the report, and every item and sentence carries a certainty score.

## Run the mock

```
pip install -r mock/requirements.txt
uvicorn mock.mock_server:app --reload --port 8000     # from the repo root
# Swagger UI: http://localhost:8000/docs
# frontend/.env.local: VITE_API_URL=http://localhost:8000/api/v1   (unset = the scaffold's built-in mock)
```

- `run_20261108_fx0001` is a finished run with data in every panel. Start here for layout work.
- `POST /runs` replays the full story live: about 8 s of planning, then it waits at approval until you `POST /approval`, then about 30 s for the rest. `MOCK_SPEED=0.5` runs it at half speed, `MOCK_SPEED=3` at triple speed.
- Stop, revoke, add source, comments, item edits and demo error injection all work.
- The fixture company is fictional ("Northwind Instruments (fixture)", USD). The real demo company reports in EUR. Format from `value.unit`, never hardcode a currency.

## Moving the scaffold onto the contract (do this first)

Most of the change stays inside `src/api/`, because the rest of the app goes through the `Api` interface.

1. **Types.** Vite won't serve files from outside `frontend/` by default, so copy the generated types in. Add a script to `package.json`: `"contract": "node -e \"require('fs').copyFileSync('../contract/types.ts','src/api/contract.ts')\""`. Run `npm run contract` after every merge from `main`, and have `src/api/types.ts` re-export what the app needs from `./contract`. Never edit `contract.ts` by hand.
2. **Endpoints.** Map the draft calls in `http.ts` onto the contract:

| Draft (`docs/API.md`) | Contract (`docs/CONTRACT.md`) |
|---|---|
| `POST /runs` returns `RunDetail` | `POST /runs` returns `Run`, which has `stage` instead of `steps` |
| `GET /runs` returns `Run[]` | `GET /runs` returns `{runs}` |
| `GET /runs/{id}/events?after=` returns `{events}` | Same path, returns `EventsPage`: `{events, last_seq, run_status, stage}` |
| `GET /runs/{id}/sources` | `GET /runs/{id}/plan` returns `{needs, sources, ready}` |
| `PATCH` tier, `PUT` order, `DELETE`, `POST sources/approve` | One `POST /runs/{id}/approval` with `decisions[]` (approved or rejected, plus tier and rank). Keep the edits local until "Approve & run" |
| none | `POST /runs/{id}/sources/{sid}/revoke`, after approval |
| `GET /runs/{id}/claims` | The items inside `GET /runs/{id}/report` |
| `GET /runs/{id}/report` | `GET /runs/{id}/summary` for the brief, `GET /runs/{id}/report` for the agent report |

3. **Field names.** Tiers `1`, `2`, `3` become `priority`, `tier_2`, `tier_3`. A `Claim` becomes one of `DataItem`, `Conclusion`, `Extrapolation` or `Chart` (field `kind`). `code_check` and `agent_check` become `verification.checks` (method `code` or `llm_judge`) plus `verification.status`. Event `decision: allow | deny` becomes `gateway.request` with `data.decision` (`allowed`, `blocked`, `flagged`), and event `summary` becomes `message`. Bull, base and bear become BOFFIN scores (`Extrapolation.score` of -1, 0 or +1). The `[S1]` markers in report text become `derived_from` on each brief sentence.
4. **Pipeline view.** Keep the 12 steps if you like them, and drive them from `Run.stage`:

| `Run.stage` | Draft steps |
|---|---|
| `plan` | 1 to 3 |
| `approve` | 4 |
| `collect` | 5 |
| `extract` | 7, plus the data part of 6 (data is checked before anything is drafted) |
| `draft` | 6 (conclusions and scores) |
| `verify`, `repair` | 8 and 9 |
| `summarize` | 10 |
| `verify_summary` | 11 |
| `complete` | 12 |

5. **The built-in mock** (`src/api/mock.ts`). Use the Python mock server, because every one of its responses is validated against the contract. If you want an offline fallback, rebuild `mock.ts` from `mock/fixtures/*.json` instead of keeping hand-written data.

## Layout (think Adobe Lightroom panels plus a VS Code bottom drawer)

The scaffold and `docs/DESIGN.md` may already settle the look, so keep what works. These are the pieces the demo needs.

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

- Types come from `contract/types.ts`, copied in by `npm run contract`. It's generated, so never edit it. Never edit `contract/`, `mock/` or `backend/`, and never edit the shared docs on the `frontend` branch. If you need a shape change, ask Gordon. He changes `models.py` on `main` first, then you merge `main`.
- Never call AWS from the browser. All calls go to `VITE_API_URL`.
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
