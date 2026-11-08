"use client";

import { useEffect, useMemo, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import type { Agent, AgentRole, VaultFile, VaultNode } from "@contract/types";
import { api } from "@/lib/api";
import { count, fmtTime, usd } from "@/lib/format";
import { useRunStore } from "@/lib/store";
import { useWindows } from "@/lib/windows";

// The four live panels that used to share the bottom drawer. Each is its own window now.

const DECISION_CLS = { allowed: "", blocked: "bg-row-blocked text-desk-bad", flagged: "bg-row-flagged text-desk-warn" } as const;

export function Traffic() {
  const traffic = useRunStore((s) => s.traffic);
  const openWin = useWindows((s) => s.open);
  const [onlyBad, setOnlyBad] = useState(false);
  const rows = useMemo(() => {
    const r = onlyBad ? traffic.filter((t) => t.data.decision !== "allowed") : traffic;
    return r.slice(-600).reverse();
  }, [traffic, onlyBad]);
  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-none flex-wrap items-center gap-x-3 gap-y-1 border-b border-desk-line bg-desk px-3 py-1.5">
        <label className="flex items-center gap-1.5 whitespace-nowrap">
          <input type="checkbox" checked={onlyBad} onChange={(e) => setOnlyBad(e.target.checked)} />
          Show only blocked and flagged
        </label>
        <span className="text-desk-muted">Every agent request goes through the gateway. Only approved sources are allowed; everything else is denied.</span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
      <table className="w-full border-collapse">
        <thead className="sticky top-0 z-10 bg-desk text-left text-[11px] text-desk-muted">
          <tr>
            {["Time", "Agent", "Role", "Action", "Target", "Decision", "Rule", "ms", "Tokens in/out", "Cost"].map((h, i) => (
              <th key={h} className={`px-2 py-1 font-medium shadow-[inset_0_-1px_0_var(--color-desk-line)] ${i >= 7 ? "text-right" : ""}`}>
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
                    <button onClick={() => openWin("agent", e.agent_id!)} className="hover:text-white hover:underline" title={`Open the chat with ${e.agent_id}`}>
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
    </div>
  );
}

const ROLES: AgentRole[] = ["planner", "fetcher", "extractor", "drafter", "verifier", "repairer", "summarizer", "reviser"];
const AGENT_CLS = { queued: "bg-desk-line-strong", working: "bg-desk-link animate-pulse", done: "bg-desk-good", failed: "bg-desk-bad", stopped: "bg-accent" } as const;

// Click an agent to open its chat window, where you can talk to it or stop it.
export function Agents() {
  const agents = useRunStore((s) => s.agents);
  const openWin = useWindows((s) => s.open);
  // Agents that already have a chat window get an outline.
  const chats = useWindows(useShallow((s) => Object.values(s.wins).flatMap((w) => (w.kind === "agent" && w.ref ? [w.ref] : []))));
  const byRole = useMemo(() => {
    const m = new Map<AgentRole, Agent[]>();
    for (const a of Object.values(agents)) m.set(a.role, [...(m.get(a.role) ?? []), a]);
    return m;
  }, [agents]);
  return (
    <div className="p-3">
      {ROLES.filter((r) => byRole.has(r)).map((r) => (
        <div key={r} className="mb-2 flex items-start gap-3">
          <span className="w-24 flex-none text-desk-muted">
            {r} <span className="font-mono">{byRole.get(r)!.length}</span>
          </span>
          <div className="flex flex-wrap">
            {byRole.get(r)!.map((a) => (
              <button
                key={a.agent_id}
                aria-label={`${a.agent_id} ${a.status}, open chat`}
                title={`${a.agent_id}: ${a.status}. ${a.task}`}
                onClick={() => openWin("agent", a.agent_id)}
                className="grid h-6 w-6 place-items-center rounded-[4px] hover:bg-desk-raised"
              >
                <span className={`h-3 w-3 rounded-full transition-colors ${AGENT_CLS[a.status]} ${chats.includes(a.agent_id) ? "outline-2 outline-offset-1 outline-desk-ink" : ""}`} />
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
        <span className="ml-auto">Click an agent to open its chat, where you can also stop it.</span>
      </div>
    </div>
  );
}

export function Vault() {
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
      <div className="w-[40%] max-w-80 min-w-40 flex-none overflow-auto border-r border-desk-line p-2">
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
      <button onClick={() => onOpen(n.path)} style={{ paddingLeft: depth * 12 }} className={`block w-full truncate text-left hover:text-white ${active === n.path ? "text-accent" : ""}`}>
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

export function Log() {
  const log = useRunStore((s) => s.log);
  const rows = log.slice(-800).reverse();
  return (
    <ol className="p-2">
      {rows.map((e) => (
        <li key={e.seq} className={`flex gap-2.5 py-0.5 ${e.type === "agent.log" ? "text-accent" : e.level === "error" ? "text-desk-bad" : e.level === "warn" ? "text-desk-warn" : ""}`}>
          <span className="text-desk-muted tabular-nums">{fmtTime(e.ts)}</span>
          <span className="w-28 flex-none truncate text-desk-muted">{e.agent_id ?? e.type}</span>
          <span>{e.message}</span>
        </li>
      ))}
    </ol>
  );
}
