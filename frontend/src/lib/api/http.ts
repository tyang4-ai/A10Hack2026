import type { Api } from "./types";

// Real backend client. Paths and shapes follow docs/API.md.
export function httpApi(base: string): Api {
  const root = base.replace(/\/$/, "");

  async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const res = await fetch(root + path, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`${method} ${path} failed: ${res.status} ${await res.text()}`);
    return (await res.json()) as T;
  }

  const r = (id: string) => `/runs/${encodeURIComponent(id)}`;

  return {
    createRun: (company) => call("POST", "/runs", { company }),
    listRuns: () => call("GET", "/runs"),
    getRun: (id) => call("GET", r(id)),
    listEvents: async (id, after) =>
      (await call<{ events: import("./types").RunEvent[] }>("GET", `${r(id)}/events?after=${after}`)).events,
    stopRun: (id, reason) => call("POST", `${r(id)}/stop`, { reason }),
    listSources: (id) => call("GET", `${r(id)}/sources`),
    addSource: (id, url, title) => call("POST", `${r(id)}/sources`, { url, title }),
    setSourceTier: (id, sid, tier) => call("PATCH", `${r(id)}/sources/${sid}`, { tier }),
    removeSource: (id, sid) => call("DELETE", `${r(id)}/sources/${sid}`),
    reorderSources: (id, ids) => call("PUT", `${r(id)}/sources/order`, { ids }),
    approveSources: (id) => call("POST", `${r(id)}/sources/approve`),
    listClaims: (id) => call("GET", `${r(id)}/claims`),
    getReport: (id) => call("GET", `${r(id)}/report`),
    resolveBlocked: (id, seq, action, note) => call("POST", `${r(id)}/events/${seq}/resolve`, { action, note }),
    actOnClaim: (id, cid, action, opts) => call("POST", `${r(id)}/claims/${cid}/action`, { action, ...opts }),
  };
}
