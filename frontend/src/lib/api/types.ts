// Mirrors docs/API.md. Keep the two in sync.

export type Actor = "H" | "A" | "C" | "A+H" | "A+C";
export type StepStatus = "pending" | "running" | "waiting" | "done" | "failed" | "stopped";
export type RunStatus = "running" | "awaiting_approval" | "completed" | "stopped" | "failed";

export interface Run {
  id: string;
  company: string;
  status: RunStatus;
  current_step: number;
  started_by: string;
  created_at: string;
}

export interface Step {
  n: number;
  name: string;
  actor: Actor;
  stage: 1 | 2 | 3;
  status: StepStatus;
}

export interface RunDetail extends Run {
  steps: Step[];
}

export type Tier = 1 | 2 | 3;

export interface Source {
  id: string;
  title: string;
  url: string;
  domain: string;
  tier: Tier;
  priority: number | null;
  status: "proposed" | "approved" | "fetched" | "fetch_failed" | "removed";
  approval: "human" | "auto" | null;
  added_by: "agent" | "human";
  chars: number | null;
  sha256: string | null;
}

export interface Citation {
  source_id: string;
  quote: string;
  start: number;
  found_at: number | null;
}

export type CodeCheck =
  | "pending"
  | "pass"
  | "position_off"
  | "not_found"
  | "value_mismatch"
  | "tier_violation"
  | "source_removed";

export type Verdict = "pending" | "supported" | "unsupported" | "misleading";

export interface Claim {
  id: string;
  kind: "data" | "conclusion" | "extrapolation";
  text: string;
  value: string | null;
  scenario: "bull" | "base" | "bear" | null;
  citations: Citation[];
  code_check: { status: CodeCheck; detail: string };
  agent_check: { verdict: Verdict; reason: string; validator: string | null };
}

export type EventType =
  | "step"
  | "fetch"
  | "model_call"
  | "check"
  | "approval"
  | "source_change"
  | "blocked"
  | "stop";

export interface RunEvent {
  seq: number;
  ts: string;
  step: number;
  actor: { kind: "human" | "agent" | "code" | "gateway"; id: string };
  type: EventType;
  decision: "allow" | "deny" | null;
  summary: string;
  source_id: string | null;
  claim_id: string | null;
  latency_ms: number | null;
  tokens: number | null;
}

export interface Report {
  title: string;
  generated_at: string;
  sections: { heading: string; paragraphs: { text: string; claim_ids: string[] }[] }[];
}

export interface Api {
  createRun(company: string): Promise<RunDetail>;
  listRuns(): Promise<Run[]>;
  getRun(id: string): Promise<RunDetail>;
  listEvents(id: string, after: number): Promise<RunEvent[]>;
  stopRun(id: string, reason: string): Promise<RunDetail>;
  listSources(id: string): Promise<Source[]>;
  addSource(id: string, url: string, title: string): Promise<Source>;
  setSourceTier(id: string, sid: string, tier: Tier): Promise<Source>;
  removeSource(id: string, sid: string): Promise<Source>;
  reorderSources(id: string, ids: string[]): Promise<Source[]>;
  approveSources(id: string): Promise<RunDetail>;
  listClaims(id: string): Promise<Claim[]>;
  getReport(id: string): Promise<Report | null>;
}
