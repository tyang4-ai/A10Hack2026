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

## 3. Question: is STOP resumable?

You said the kill switch is "so we can demo and retune". Do you want `POST /runs/{run_id}/resume` (status back to `running`, kill switch off, policy version bump), or is it stop and start a new run? The UI currently treats a stopped run as final.

## 4. Note on the frontend stack

The dashboard is Next.js 16, not Vite. It imports `../contract/types.ts` directly (no copy step) and reads the base URL from `NEXT_PUBLIC_API_BASE` (default `http://localhost:8000/api/v1`). The "moving the scaffold onto the contract" section in `FRONTEND_TLDR.md` is already done.
