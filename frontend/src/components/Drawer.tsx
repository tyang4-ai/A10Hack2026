"use client";

import { useEffect, useMemo, useState } from "react";
import type { Agent, AgentRole, VaultFile, VaultNode } from "@contract/types";
import { api } from "@/lib/api";
import { count, fmtTime, usd } from "@/lib/format";
import { useRunStore } from "@/lib/store";

type Tab = "traffic" | "agents" | "vault" | "log";

export function Drawer() {
  const [tab, setTab] = useState<Tab>("traffic");
  const [open, setOpen] = useState(true);
  const traffic = useRunStore((s) => s.traffic);
  const agents = useRunStore((s) => s.agents);
  const bad = traffic.filter((t) => t.data.decision !== "allowed").length;
  const tabs: [Tab, string, string][] = [
    ["traffic", "Traffic", String(traffic.length)],
    ["agents", "Agents", String(Object.keys(agents).length)],
    ["vault", "Vault", ""],
    ["log", "Log", ""],
  ];
  return (
    <section aria-label="Run drawer" className={`flex flex-none flex-col border-t border-desk-line bg-desk text-[12px] text-desk-ink ${open ? "h-[34vh] min-h-56" : ""}`}>
      <div className="flex flex-none items-center gap-1 border-b border-desk-line px-2">
        {tabs.map(([id, label, n]) => (
          <button
            key={id}
            aria-pressed={tab === id}
            onClick={() => {
              setTab(id);
              setOpen(true);
            }}
            className={`border-b-2 px-3 py-2 text-[13px] ${tab === id && open ? "border-amber font-semibold text-white" : "border-transparent text-desk-muted hover:text-white"}`}
          >
            {label} {n && <span className="ml-1 font-mono text-[11px] font-normal text-desk-muted">{n}</span>}
            {id === "traffic" && bad > 0 && <span className="ml-1.5 rounded-[3px] bg-desk-bad px-1 font-mono text-[11px] font-semibold text-desk">{bad}</span>}
          </button>
        ))}
        <button onClick={() => setOpen((o) => !o)} className="ml-auto px-2 text-desk-muted hover:text-white" aria-label={open ? "Collapse drawer" : "Expand drawer"}>
          {open ? "▾" : "▴"}
        </button>
      </div>
      {open && (
        <div className="min-h-0 flex-1 overflow-auto">
          {tab === "traffic" && <Traffic />}
          {tab === "agents" && <Agents />}
          {tab === "vault" && <Vault />}
          {tab === "log" && <Log />}
        </div>
      )}
    </section>
  );
}

const DECISION_CLS = { allowed: "", blocked: "bg-[#450A0A] text-desk-bad", flagged: "bg-[#422006] text-amber" } as const;

function Traffic() {
  const traffic = useRunStore((s) => s.traffic);
  const select = useRunStore((s) => s.select);
  const [onlyBad, setOnlyBad] = useState(false);
  const rows = useMemo(() => {
    const r = onlyBad ? traffic.filter((t) => t.data.decision !== "allowed") : traffic;
    return r.slice(-600).reverse();
  }, [traffic, onlyBad]);
  return (
    <div>
      <div className="sticky top-0 z-10 flex items-center gap-3 bg-desk px-3 py-1.5">
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={onlyBad} onChange={(e) => setOnlyBad(e.target.checked)} />
          Show only blocked and flagged
        </label>
        <span className="text-desk-muted">Every agent request goes through the gateway. Only approved sources are allowed; everything else is denied.</span>
      </div>
      <table className="w-full border-collapse">
        <thead className="sticky top-[30px] bg-desk text-left text-[11px] text-desk-muted">
          <tr>
            {["Time", "Agent", "Role", "Action", "Target", "Decision", "Rule", "ms", "Tokens in/out", "Cost"].map((h, i) => (
              <th key={h} className={`px-2 py-1 font-medium ${i >= 7 ? "text-right" : ""}`}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((e) => {
            const d = e.data;
            return (
              <tr key={e.seq} title={d.reason} className={`animate-[tape-in_240ms_ease-out] border-t border-desk-line font-mono ${DECISION_CLS[d.decision]}`}>
                <td className="px-2 py-1 whitespace-nowrap text-desk-muted tabular-nums">{fmtTime(e.ts)}</td>
                <td className="px-2 whitespace-nowrap">
                  {e.agent_id ? (
                    <button onClick={() => select({ type: "agent", id: e.agent_id! })} className="hover:text-white hover:underline" title={`Open ${e.agent_id} in the inspector`}>
                      {e.agent_id}
                    </button>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="px-2 text-desk-muted">{d.role}</td>
                <td className="px-2 whitespace-nowrap">{d.action}</td>
                <td className="max-w-[380px] truncate px-2" title={d.target}>
                  {d.target}
                </td>
                <td className="px-2 font-semibold">{d.decision.toUpperCase()}</td>
                <td className="px-2 whitespace-nowrap text-desk-muted">{d.rule_id ?? (/kill|stop/i.test(d.reason) ? "kill switch" : "default deny")}</td>
                <td className="px-2 text-right tabular-nums">{d.latency_ms}</td>
                <td className="px-2 text-right tabular-nums">{d.tokens_in !== null ? `${count(d.tokens_in)} / ${count(d.tokens_out ?? 0)}` : ""}</td>
                <td className="px-2 text-right tabular-nums">{d.cost_usd !== null ? usd(d.cost_usd) : ""}</td>
              </tr>
            );
          })}
          {rows.length === 0 && (
            <tr>
              <td colSpan={10} className="px-3 py-3 text-desk-muted">
                No requests yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

const ROLES: AgentRole[] = ["planner", "fetcher", "extractor", "drafter", "verifier", "repairer", "summarizer", "reviser"];
const AGENT_CLS = { queued: "bg-[#334155]", working: "bg-desk-link animate-pulse", done: "bg-desk-good", failed: "bg-desk-bad", stopped: "bg-amber" } as const;

// Click an agent to open it in the inspector, where you can talk to it or stop it.
function Agents() {
  const agents = useRunStore((s) => s.agents);
  const selected = useRunStore((s) => s.selected);
  const select = useRunStore((s) => s.select);
  const pick = selected?.type === "agent" ? agents[selected.id] : null;
  const byRole = useMemo(() => {
    const m = new Map<AgentRole, Agent[]>();
    for (const a of Object.values(agents)) m.set(a.role, [...(m.get(a.role) ?? []), a]);
    return m;
  }, [agents]);
  return (
    <div className="flex h-full">
      <div className="min-w-0 flex-1 p-3">
        {ROLES.filter((r) => byRole.has(r)).map((r) => (
          <div key={r} className="mb-2 flex items-start gap-3">
            <span className="w-24 flex-none text-desk-muted">
              {r} <span className="font-mono">{byRole.get(r)!.length}</span>
            </span>
            <div className="flex flex-wrap">
              {byRole.get(r)!.map((a) => (
                <button
                  key={a.agent_id}
                  aria-label={`${a.agent_id} ${a.status}`}
                  title={`${a.agent_id} · ${a.status} · ${a.task}`}
                  onClick={() => select({ type: "agent", id: a.agent_id })}
                  className="grid h-6 w-6 place-items-center rounded-[4px] hover:bg-desk-raised"
                >
                  <span className={`h-3 w-3 rounded-full transition-colors ${AGENT_CLS[a.status]} ${pick?.agent_id === a.agent_id ? "outline-2 outline-offset-1 outline-white" : ""}`} />
                </button>
              ))}
            </div>
          </div>
        ))}
        {byRole.size === 0 && <div className="text-desk-muted">No agents yet.</div>}
        <div className="mt-2 flex flex-wrap gap-3 text-[11px] text-desk-muted">
          {Object.entries(AGENT_CLS).map(([k, c]) => (
            <span key={k} className="flex items-center gap-1">
              <span className={`h-2 w-2 rounded-full ${c.replace("animate-pulse", "")}`} />
              {k}
            </span>
          ))}
          <span className="ml-auto">Click an agent to talk to it or stop it in the inspector.</span>
        </div>
      </div>
      {pick && (
        <div className="w-72 flex-none border-l border-desk-line p-3">
          <div className="font-mono text-white">{pick.agent_id}</div>
          <div className="text-desk-muted">
            {pick.role}, {pick.model_id ?? "code only"}, {pick.status}
          </div>
          <div className="mt-2">{pick.task}</div>
          <div className="mt-2 text-desk-muted">
            {pick.usage.model_calls} model calls, {count(pick.usage.tokens_in + pick.usage.tokens_out)} tokens, {usd(pick.usage.cost_usd)}
          </div>
        </div>
      )}
    </div>
  );
}

function Vault() {
  const runId = useRunStore((s) => s.runId)!;
  const [tree, setTree] = useState<VaultNode | null>(null);
  const [file, setFile] = useState<VaultFile | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.vaultTree(runId).then(setTree, (e) => setErr(String(e)));
  }, [runId]);
  const open = (p: string) => api.vaultFile(runId, p).then(setFile, (e) => setErr(String(e)));
  return (
    <div className="flex h-full">
      <div className="w-80 flex-none overflow-auto border-r border-desk-line p-2">
        {err && <div className="text-desk-bad">{err}</div>}
        {tree ? <Node n={tree} depth={0} onOpen={open} active={file?.path} /> : <div aria-busy="true" className="text-desk-muted">Loading the vault</div>}
      </div>
      <div className="min-w-0 flex-1 overflow-auto p-3">
        {file ? (
          <>
            <div className="mb-2 text-desk-muted">
              {file.path} ({file.content_type}, {file.size.toLocaleString()} bytes)
            </div>
            {file.text !== null ? (
              <pre className="whitespace-pre-wrap text-desk-ink">
                {file.text}
                {file.truncated && "\n… truncated"}
              </pre>
            ) : (
              <span className="text-desk-muted">Binary file{file.download_url ? "" : ", no preview"}.</span>
            )}
          </>
        ) : (
          <span className="text-desk-muted">Raw downloads, extracted text, agent transcripts and verification files. Pick a file to preview it.</span>
        )}
      </div>
    </div>
  );
}

function Node({ n, depth, onOpen, active }: { n: VaultNode; depth: number; onOpen: (p: string) => void; active?: string }) {
  const [open, setOpen] = useState(depth < 2);
  if (n.type === "file")
    return (
      <button onClick={() => onOpen(n.path)} style={{ paddingLeft: depth * 12 }} className={`block w-full truncate text-left hover:text-white ${active === n.path ? "text-amber" : ""}`}>
        {n.name}
      </button>
    );
  return (
    <div>
      <button onClick={() => setOpen((o) => !o)} style={{ paddingLeft: depth * 12 }} className="block w-full truncate text-left text-desk-muted hover:text-white">
        {open ? "▾" : "▸"} {n.name}
      </button>
      {open && n.children?.map((c) => <Node key={c.path} n={c} depth={depth + 1} onOpen={onOpen} active={active} />)}
    </div>
  );
}

function Log() {
  const log = useRunStore((s) => s.log);
  const rows = log.slice(-800).reverse();
  return (
    <ol className="p-2">
      {rows.map((e) => (
        <li key={e.seq} className={`flex gap-2.5 py-0.5 ${e.type === "agent.log" ? "text-amber" : e.level === "error" ? "text-desk-bad" : e.level === "warn" ? "text-amber" : ""}`}>
          <span className="text-desk-muted tabular-nums">{fmtTime(e.ts)}</span>
          <span className="w-28 flex-none truncate text-desk-muted">{e.agent_id ?? e.type}</span>
          <span>{e.message}</span>
        </li>
      ))}
    </ol>
  );
}
