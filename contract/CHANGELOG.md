# Contract changelog

Newest first. Every shape change gets a line here and a bump in `contract/VERSION`.

## 0.3.1 (Sun Nov 8, 2026, 2:25 AM)

Additive, asked for in the backend session (delete runs from the home page, real PDF pages in the viewer).
- `DELETE /runs/{run_id}` returns `RunDeleted` (`run_id`, `events`, `vault_files`, `note`). Only a finished run. 409 while it runs, for replays and for protected runs. The shared document cache and recorded model answers stay.
- `GET /runs/{run_id}/documents/{doc_id}/pages/{page}/image?item_id=` returns `image/png`: the stored PDF page with the item's quotes highlighted. 404 when no PDF is stored, and always in the mock, so the viewer falls back to the text page.
- The mock answers both routes. Event types: 29 (unchanged).

## 0.3.0 (Sat Nov 7, 2026, 11:45 PM)

All additive, from Gordon's notes and Tong's request to rerun one agent (`frontend/CONTRACT_REQUESTS.md` section 4).
- Bobby, the orchestrator: role `orchestrator`, `Agent.name` ("Bobby", "Bobby's goon #n"), event `agent.decision` (`AgentDecisionData`, `ReworkOrder`).
- Rerun one agent in the same run: `POST /runs/{id}/agents/{agent_id}/rerun`, `AgentRerunRequest`, `Agent.parent_agent_id`, `Agent.instructions`, `Agent.rerun_by`, `item.updated` reason `rework`, repair action `rework`.
- Fixtures: Bobby spawns first, logs four calls, and every other agent is one of his goons. The mock reruns one agent with new item versions and re-checks. Event types: 29.
- Frontend heads-up: `AgentRole` gained `orchestrator`, so a `Record<AgentRole, ...>` needs that key.

## 0.2.0 (Sat Nov 7, 2026, 11 PM)

All additive. From Tong's frontend requests (`frontend/CONTRACT_REQUESTS.md`), approved by Gordon, plus the backend's `source_revoked` request and the demo cache labels.
- Stop one agent: `POST /runs/{id}/agents/{agent_id}/stop`, `PolicyRule.agent_id`, rule origin `user_agent_stop`, `AgentStatusData.reason`.
- Agent chat: `AgentMessage`, `AgentMessageRequest`, `AgentMessageList`, `GET`/`POST /runs/{id}/agents/{agent_id}/messages`, event `agent.message`.
- Resume after STOP: `POST /runs/{id}/resume`, `ResumeRequest`, event `run.resumed`.
- Rerun from a step: `POST /runs/{id}/rerun`, `RerunRequest`, `Run.parent_run_id`, `Run.rerun_from`, `Run.instructions`.
- `item.updated` reason `source_revoked`.
- Demo cache labels: `Run.cache_mode`, `GatewayRequestData.cache` and `cached_at`, `Document.origin` and `collected_at`.
- The mock answers every new route. Event types: 28.

## 0.1.0 (Sat Nov 7, 2026)

First version, merged with ideas from Tong's API and design drafts (code-assigned tiers, the `tier_rule` and `context` checks). Covers runs, plan and approval, gateway policy, documents and page highlights, report items (data, conclusions, BOFFIN scores, charts), verification with repair history and diffs, certainty per item, per sentence and per report, the 3-page brief, events, agents, comments, vault, demo controls.
