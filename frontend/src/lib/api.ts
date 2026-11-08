// Thin client for the Axelrod API. Shapes come from the shared contract
// (../contract/types.ts, generated from contract/models.py). Never call AWS here.
import type {
  AddSourceRequest,
  AgentList,
  AgentReport,
  ApprovalRequest,
  ApprovalResponse,
  Comment,
  CommentList,
  CommentRequest,
  CreateRunRequest,
  DocumentPage,
  EventsPage,
  HumanReport,
  InjectErrorsResponse,
  Plan,
  Policy,
  RevokeResponse,
  Run,
  RunList,
  Source,
  VaultFile,
  VaultNode,
} from "@contract/types";

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000/api/v1";

// The contract's error envelope: {"error": {"code", "message", "details"}}.
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details: Record<string, unknown> | null,
  ) {
    super(message);
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(API_BASE + path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const json = await res.json().catch(() => null);
  if (!res.ok) {
    const e = json?.error;
    throw new ApiError(res.status, e?.code ?? "http_error", e?.message ?? `${method} ${path} failed (${res.status})`, e?.details ?? null);
  }
  return json as T;
}

const r = (id: string) => `/runs/${encodeURIComponent(id)}`;

export const api = {
  listRuns: () => call<RunList>("GET", "/runs"),
  createRun: (req: CreateRunRequest) => call<Run>("POST", "/runs", req),
  getRun: (id: string) => call<Run>("GET", r(id)),
  events: (id: string, after: number, limit = 500) => call<EventsPage>("GET", `${r(id)}/events?after=${after}&limit=${limit}`),
  plan: (id: string) => call<Plan>("GET", `${r(id)}/plan`),
  approve: (id: string, req: ApprovalRequest) => call<ApprovalResponse>("POST", `${r(id)}/approval`, req),
  stop: (id: string, reason: string) => call<Run>("POST", `${r(id)}/stop`, { reason }),
  revoke: (id: string, sid: string, reason: string) => call<RevokeResponse>("POST", `${r(id)}/sources/${sid}/revoke`, { reason }),
  addSource: (id: string, req: AddSourceRequest) => call<Source>("POST", `${r(id)}/sources`, req),
  policy: (id: string) => call<Policy>("GET", `${r(id)}/policy`),
  report: (id: string) => call<AgentReport>("GET", `${r(id)}/report`),
  summary: (id: string) => call<HumanReport>("GET", `${r(id)}/summary`),
  page: (id: string, doc: string, page: number) => call<DocumentPage>("GET", `${r(id)}/documents/${doc}/pages/${page}`),
  agents: (id: string) => call<AgentList>("GET", `${r(id)}/agents`),
  vaultTree: (id: string) => call<VaultNode>("GET", `${r(id)}/vault/tree`),
  vaultFile: (id: string, path: string) => call<VaultFile>("GET", `${r(id)}/vault/file?path=${encodeURIComponent(path)}`),
  comments: (id: string) => call<CommentList>("GET", `${r(id)}/comments`),
  comment: (id: string, req: CommentRequest) => call<Comment>("POST", `${r(id)}/comments`, req),
  injectErrors: (id: string, count: number) => call<InjectErrorsResponse>("POST", `${r(id)}/demo/inject-errors`, { count }),
};
