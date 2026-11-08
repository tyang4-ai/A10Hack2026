# Contract requests from the frontend

Shapes the dashboard needs that are not in `contract/` yet. The UI is already built against these and shows "not supported by this backend yet" until they exist. If you change a name, tell me and I'll match it. The provisional TypeScript lives in `frontend/src/lib/agentApi.ts` and gets deleted once these are in `models.py`.

## 1. Talk to an individual agent

Any agent the harness spawned can be messaged, like a support chat but per agent. The agent answers from its own task and context.

```python
class AgentMessage(BaseModel):
    message_id: str          # "AM-001"
    agent_id: str
    author: Literal["analyst", "agent"]
    text: str
    status: Literal["sent", "answered", "failed"]   # analyst messages: sent → answered
    created_at: str
```

| Method and path | Body | Returns |
|---|---|---|
| `POST /runs/{run_id}/agents/{agent_id}/messages` | `{"text": "..."}` | `AgentMessage` (201), the analyst's message |
| `GET /runs/{run_id}/agents/{agent_id}/messages` | | `{"messages": AgentMessage[]}`, oldest first |

New event `agent.message` with `data: {message: AgentMessage}`, fired for both the analyst's message and the agent's reply, so replies stream in through the normal polling.

Errors: 404 unknown agent, 409 if the run is stopped (chatting with a finished agent is fine if you can support it).

## 2. Stop one agent

| Method and path | Body | Returns |
|---|---|---|
| `POST /runs/{run_id}/agents/{agent_id}/stop` | `{"reason": "..."}` | `Agent` with `status: "stopped"` |

Expected effects: an `agent.status` event with `stopped`, a deny rule for that agent in the gateway (for example `R-stop-extractor-03`) with a `policy.updated` event, and the next request from that agent shows as `blocked` in traffic. The rest of the run keeps going. 409 if the agent already finished or the run is stopped.

## 3. Rerun from a step with new instructions

The analyst clicks a finished step in the stage bar (or the step a stopped or failed run halted on), types instructions, and starts a rerun. A rerun is a fork, not a rewind: it creates a new run that reuses everything before `from_stage` and redoes `from_stage` onward with the instructions. The original run is never changed, so its audit trail stays intact and the two can be compared.

| Method and path | Body | Returns |
|---|---|---|
| `POST /runs/{run_id}/rerun` | `{"from_stage": Stage, "instructions": "..."}` | the new `Run` (201) |

Three new optional fields on `Run`, null for normal runs:

```python
parent_run_id: str | None = None      # the run this was forked from
rerun_from: Stage | None = None       # the step it reran from
instructions: str | None = None       # the analyst's instructions, passed to every agent from that step on
```

What the new run keeps, by `from_stage`:

| from_stage | Kept from the parent | Redone |
|---|---|---|
| `plan` | nothing | everything, with instructions to the planner |
| `approve` | needs and proposed sources | the approval gate (status `awaiting_approval`) |
| `collect` | plan and approvals | fetching |
| `extract` | stored documents | extraction onward |
| `draft` | verified data items | conclusions and scores onward |
| `verify` | the draft | checks onward |
| `repair` | checked items | repair onward |
| `summarize` | the verified agent report | the brief |
| `verify_summary` | the brief | brief checks |

Copied items keep their ids, so citations still resolve. The new run gets its own policy (same rules as the parent at that point) and its own event log starting at seq 1. A `run.created` style first event that says which run and step it came from would help the log window.

Errors: 400 for `complete` or a step the parent never reached, 409 if the parent is still running and you'd rather not fork a live run (the UI allows it for finished steps, but either is fine).

## 4. Question: is STOP resumable?

You said the kill switch is "so we can demo and retune". Do you want `POST /runs/{run_id}/resume` (status back to `running`, kill switch off, policy version bump), or is it stop and start a new run? The UI currently treats a stopped run as final.

## 5. Note on the frontend stack

The dashboard is Next.js 16, not Vite. It imports `../contract/types.ts` directly (no copy step) and reads the base URL from `NEXT_PUBLIC_API_BASE` (default `http://localhost:8000/api/v1`). The "moving the scaffold onto the contract" section in `FRONTEND_TLDR.md` is already done.
