// Thin client for the Axelrod API, the same endpoints the web dashboard uses.
// Shapes come from the shared contract (contract/types.ts, generated from contract/models.py).
// The base URL comes from AXELROD_API (the same variable as the backend's Python CLI),
// then localhost. AXELROD_API_KEY is sent as X-Axelrod-Key when set, and never printed.
import type {
  AgentList,
  AgentReport,
  ApprovalRequest,
  ApprovalResponse,
  DocumentPage,
  EventsPage,
  HumanReport,
  Plan,
  Policy,
  Run,
  RunList,
} from "../../../contract/types.ts";

const raw = (process.env.AXELROD_API ?? process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000/api/v1").trim().replace(/\/+$/, "");
export const API_BASE = raw.endsWith("/api/v1") ? raw : `${raw}/api/v1`;

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (process.env.AXELROD_API_KEY) headers["X-Axelrod-Key"] = process.env.AXELROD_API_KEY;
  let res: Response;
  try {
    res = await fetch(API_BASE + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, "unreachable", `Can't reach the Axelrod server at ${API_BASE}.`);
  }
  const json = await res.json().catch(() => null);
  if (!res.ok) {
    const e = json?.error;
    throw new ApiError(res.status, e?.code ?? "http_error", e?.message ?? `${method} ${path} failed (${res.status})`);
  }
  return json as T;
}

const r = (id: string) => `/runs/${encodeURIComponent(id)}`;

export const api = {
  listRuns: () => call<RunList>("GET", "/runs"),
  createRun: (company: string) => call<Run>("POST", "/runs", { company, created_via: "cli" }),
  getRun: (id: string) => call<Run>("GET", r(id)),
  events: (id: string, after: number) => call<EventsPage>("GET", `${r(id)}/events?after=${after}&limit=500`),
  plan: (id: string) => call<Plan>("GET", `${r(id)}/plan`),
  approve: (id: string, req: ApprovalRequest) => call<ApprovalResponse>("POST", `${r(id)}/approval`, req),
  stop: (id: string, reason: string) => call<Run>("POST", `${r(id)}/stop`, { reason }),
  resume: (id: string, reason: string) => call<Run>("POST", `${r(id)}/resume`, { reason }),
  report: (id: string) => call<AgentReport>("GET", `${r(id)}/report`),
  summary: (id: string) => call<HumanReport>("GET", `${r(id)}/summary`),
  agents: (id: string) => call<AgentList>("GET", `${r(id)}/agents`),
  policy: (id: string) => call<Policy>("GET", `${r(id)}/policy`),
  page: (id: string, doc: string, page: number) => call<DocumentPage>("GET", `${r(id)}/documents/${doc}/pages/${page}`),
};
