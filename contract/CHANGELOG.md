# Contract changelog

Newest first. Every shape change gets a line here and a bump in `contract/VERSION`.

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
