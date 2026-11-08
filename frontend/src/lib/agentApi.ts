// PROVISIONAL: agent chat and per-agent stop are not in contract/ yet.
// These shapes are the frontend's proposal (see frontend/CONTRACT_REQUESTS.md).
// Delete this file and import from @contract/types once Gordon adds them to models.py.
import type { Agent } from "@contract/types";
import { API_BASE, ApiError } from "./api";

export interface AgentMessage {
  message_id: string;
  agent_id: string;
  author: "analyst" | "agent";
  text: string;
  status: "sent" | "answered" | "failed";
  created_at: string;
}

// Thrown when the backend has no such endpoint yet (404 without the contract's error envelope).
export class NotSupported extends Error {}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(API_BASE + path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const json = await res.json().catch(() => null);
  if (res.status === 404 && !json?.error) throw new NotSupported(`${method} ${path} is not implemented by this backend yet`);
  if (!res.ok) {
    const e = json?.error;
    throw new ApiError(res.status, e?.code ?? "http_error", e?.message ?? `${method} ${path} failed (${res.status})`, e?.details ?? null);
  }
  return json as T;
}

const a = (run: string, agent: string) => `/runs/${encodeURIComponent(run)}/agents/${encodeURIComponent(agent)}`;

export const agentApi = {
  messages: (run: string, agent: string) => call<{ messages: AgentMessage[] }>("GET", `${a(run, agent)}/messages`),
  send: (run: string, agent: string, text: string) => call<AgentMessage>("POST", `${a(run, agent)}/messages`, { text }),
  stop: (run: string, agent: string, reason: string) => call<Agent>("POST", `${a(run, agent)}/stop`, { reason }),
};

// The provisional "agent.message" event, read defensively because it is not in the Event union yet.
export function agentMessageFromEvent(e: { type: string; data?: unknown }): AgentMessage | null {
  if (e.type !== "agent.message") return null;
  const m = (e.data as { message?: AgentMessage } | undefined)?.message;
  return m && typeof m.message_id === "string" ? m : null;
}
