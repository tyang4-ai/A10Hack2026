# API contract (dashboard ↔ backend)

Draft contract so the dashboard and backend can be built in parallel. The dashboard currently runs against an in-browser mock that follows this contract (`frontend/src/lib/api/mock.ts`). Types live in `frontend/src/lib/api/types.ts`; keep the two in sync.

- Base URL: `NEXT_PUBLIC_API_URL` (e.g. the API Gateway stage URL). If unset, the dashboard uses the mock.
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
| POST | `/runs/{id}/events/{seq}/resolve` | `{ "action": "allow" \| "add_source" \| "dismiss", "note": "..." }` | `Event` (the new `resolution` event) |
| POST | `/runs/{id}/claims/{cid}/action` | `{ "action": "send_back" \| "promote_source" \| "edit" \| "drop", "note": "...", "text"?: "..." }` | `Claim` |

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
  resolution: Resolution | null; // set once a person acts on a failed claim
}

interface Resolution {
  action: string;                // the ClaimAction chosen
  by: string;
  note: string;                  // note to the agent, may be ""
  at: string;
  state?: "working" | "done";   // "working" while the agent fixes it
}

interface Event {
  seq: number;                   // strictly increasing per run
  ts: string;
  step: number;
  actor: { kind: "human" | "agent" | "code" | "gateway"; id: string };  // e.g. "validator:S3"
  type: "step" | "fetch" | "model_call" | "check" | "approval" | "source_change" | "blocked" | "resolution" | "stop";
  decision: "allow" | "deny" | null;
  summary: string;
  source_id: string | null;
  claim_id: string | null;
  url: string | null;            // address of a blocked fetch
  resolves: number | null;       // on a "resolution" event: seq of the blocked event it settles
  action: string | null;         // on a "resolution" event: what the person chose
  latency_ms: number | null;
  tokens: number | null;
}

interface Report {
  title: string;
  generated_at: string;
  sections: { heading: string; paragraphs: { text: string; claim_ids: string[] }[] }[];
  // text uses [S1]-style markers that match claim citations
  charts: ChartSpec[];
}

// Generative UI: the agent picks the chart type and which claims to plot.
// Points name claim ids, never numbers; the dashboard draws the verified value
// and withholds the whole chart if any point's claim failed, was dropped or is being fixed.
interface ChartSpec {
  id: string;                    // "F1"
  type: "line" | "waterfall" | "fan" | "stacked_bar";
  title: string;
  unit: string;
  caption: string;
  points: { label: string; claim_id: string; role?: "total" | "delta" | "start" | "bull" | "base" | "bear" }[];
  lint: { check: string; ok: boolean }[];  // chart checks run by code, e.g. "zero baseline"
}
```

## Blocking rules the dashboard expects to see as `blocked` events
- Fetch of a domain that isn't an approved source.
- A data claim whose only support is a tier 3 source.
- An agent trying to change a source's tier.
- Any action after the run is stopped.

## Acting on blocked actions and failed claims
The event log is append-only: a person's decision is a new `resolution` event whose `resolves` points at the blocked event. The dashboard treats a blocked event as open until such an event exists.

| Blocked event | Actions | Backend effect |
|---|---|---|
| Agent tried to change a tier (`source_id` set) | `allow`, `dismiss` | `allow` promotes the source to tier 1 as a human change |
| Fetch of an unapproved site (`url` set) | `add_source`, `dismiss` | `add_source` adds the URL as a source; tier rules apply |

| Failed claim | Actions | Backend effect |
|---|---|---|
| `not_found`, `value_mismatch`, `source_removed` | `send_back`, `drop` | `send_back` asks the agent to find real support, then re-runs the quote check and validator |
| `tier_violation` | `send_back`, `promote_source`, `drop` | `promote_source` raises the cited source to tier 1, then re-checks |
| verdict `misleading` / `unsupported` | `send_back`, `edit` (conclusions and extrapolations only, with `text`), `drop` | `edit` replaces the wording, then the validator re-reads it |

While the agent works, set `resolution.state = "working"` and `code_check.status = "pending"`; set it to `"done"` when checks finish. Dropped claims stay in `/claims` but never appear in the report. Every action also appends a `resolution` event (with `claim_id`) so it shows in the tape and the audit trail. `note` is passed to the agent as the person's instruction.
