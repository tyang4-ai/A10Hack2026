# API contract (dashboard ↔ backend)

Draft contract so the dashboard and backend can be built in parallel. The dashboard currently runs against an in-browser mock that follows this contract (`frontend/src/api/mock.ts`). Types live in `frontend/src/api/types.ts`; keep the two in sync.

- Base URL: `VITE_API_URL` (e.g. the API Gateway stage URL). If unset, the dashboard uses the mock.
- JSON everywhere, `snake_case` fields, ISO-8601 UTC timestamps.
- Live updates use polling (`GET /runs/{id}/events?after=`), which works with API Gateway + Lambda. A WebSocket can replace it later without changing the event shape.

## Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/runs` | `{ "company": "Siemens Healthineers" }` | `RunDetail` |
| GET | `/runs` | | `Run[]` |
| GET | `/runs/{id}` | | `RunDetail` |
| GET | `/runs/{id}/events?after={seq}` | | `{ "events": Event[] }` (seq > after, ascending) |
| POST | `/runs/{id}/stop` | `{ "reason": "..." }` | `RunDetail` |
| GET | `/runs/{id}/sources` | | `Source[]` |
| POST | `/runs/{id}/sources` | `{ "url", "title" }` | `Source` (tier assigned by code) |
| PATCH | `/runs/{id}/sources/{sid}` | `{ "tier"?: 1\|2\|3 }` | `Source` |
| DELETE | `/runs/{id}/sources/{sid}` | | `Source` (status `removed`) |
| PUT | `/runs/{id}/sources/order` | `{ "ids": ["S1","S3",...] }` | `Source[]` (tier 1 priority order) |
| POST | `/runs/{id}/sources/approve` | | `RunDetail` (resumes step 4 → 5) |
| GET | `/runs/{id}/claims` | | `Claim[]` |
| GET | `/runs/{id}/report` | | `Report \| null` |

## Types

```ts
type Actor = "H" | "A" | "C" | "A+H" | "A+C";
type StepStatus = "pending" | "running" | "waiting" | "done" | "failed" | "stopped";
type RunStatus = "running" | "awaiting_approval" | "completed" | "stopped" | "failed";

interface Run {
  id: string;
  company: string;
  status: RunStatus;
  current_step: number;          // 1-12
  started_by: string;
  created_at: string;
}

interface Step {
  n: number;                     // 1-12
  name: string;
  actor: Actor;
  stage: 1 | 2 | 3;
  status: StepStatus;
}

interface RunDetail extends Run { steps: Step[]; }

interface Source {
  id: string;                    // "S1"
  title: string;
  url: string;
  domain: string;
  tier: 1 | 2 | 3;
  priority: number | null;       // order within tier 1, 1 = highest
  status: "proposed" | "approved" | "fetched" | "fetch_failed" | "removed";
  approval: "human" | "auto" | null;
  added_by: "agent" | "human";
  chars: number | null;          // stored extracted text length
  sha256: string | null;
}

interface Citation {
  source_id: string;
  quote: string;
  start: number;                 // position the agent claimed
  found_at: number | null;       // position code actually found, null if not found
}

type CodeCheck =
  | "pending" | "pass" | "position_off" | "not_found"
  | "value_mismatch" | "tier_violation" | "source_removed";

interface Claim {
  id: string;                    // "C1"
  kind: "data" | "conclusion" | "extrapolation";
  text: string;
  value: string | null;          // the number, for data claims
  scenario: "bull" | "base" | "bear" | null;
  citations: Citation[];
  code_check: { status: CodeCheck; detail: string };
  agent_check: { verdict: "pending" | "supported" | "unsupported" | "misleading"; reason: string; validator: string | null };
}

interface Event {
  seq: number;                   // strictly increasing per run
  ts: string;
  step: number;
  actor: { kind: "human" | "agent" | "code" | "gateway"; id: string };  // e.g. "validator:S3"
  type: "step" | "fetch" | "model_call" | "check" | "approval" | "source_change" | "blocked" | "stop";
  decision: "allow" | "deny" | null;
  summary: string;
  source_id: string | null;
  claim_id: string | null;
  latency_ms: number | null;
  tokens: number | null;
}

interface Report {
  title: string;
  generated_at: string;
  sections: { heading: string; paragraphs: { text: string; claim_ids: string[] }[] }[];
  // text uses [S1]-style markers that match claim citations
}
```

## Blocking rules the dashboard expects to see as `blocked` events
- Fetch of a domain that isn't an approved source.
- A data claim whose only support is a tier 3 source.
- An agent trying to change a source's tier.
- Any action after the run is stopped.
