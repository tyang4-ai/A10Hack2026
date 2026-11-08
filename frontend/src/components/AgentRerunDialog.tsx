"use client";

// Rerun one agent with new instructions. Unlike rerunning from a step (which forks a new
// run), this stays in the same run: the harness spawns a new agent linked to this one, its
// outputs replace the old ones as new item versions, and with re-check on, everything that
// cites them is checked again. The gateway still enforces the same policy on the new agent.
import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { AgentRole } from "@contract/types";
import { agentApi, NotSupported } from "@/lib/agentApi";
import { allSentences, useRunStore } from "@/lib/store";
import { useWindows } from "@/lib/windows";
import { citesOf } from "./Inspector";

const EXAMPLE: Record<AgentRole, string> = {
  planner: "Add a data need for customer concentration.",
  fetcher: "Fetch the whole 10-K, not only the first section.",
  extractor: "Take revenue from the income statement, not the press release.",
  drafter: "Be stricter on secular growth, and say why when the data is thin.",
  verifier: "Treat rounding differences over 1% as failures.",
  repairer: "Downgrade rather than delete items you can't re-cite.",
  summarizer: "Lead with the leverage risk.",
  reviser: "Keep the original wording where the check passed.",
};

export function AgentRerunDialog({ agentId, onClose }: { agentId: string; onClose: () => void }) {
  const runId = useRunStore((s) => s.runId)!;
  const agent = useRunStore((s) => s.agents[agentId]);
  const items = useRunStore((s) => s.items);
  const summary = useRunStore((s) => s.summary);
  const [text, setText] = useState("");
  const [recheck, setRecheck] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [unsupported, setUnsupported] = useState(false);

  // What this agent produced, and everything downstream that a re-check would revisit.
  const { produced, downstream, sentences } = useMemo(() => {
    const produced = Object.values(items)
      .filter((i) => "agent_id" in i && i.agent_id === agentId) // charts are drawn by code, not an agent
      .map((i) => i.id);
    const reached = new Set(produced);
    for (let grew = true; grew; ) {
      grew = false;
      for (const it of Object.values(items)) {
        if (reached.has(it.id)) continue;
        if (citesOf(it).some((r) => reached.has(r.split(":")[0]))) {
          reached.add(it.id);
          grew = true;
        }
      }
    }
    const downstream = [...reached].filter((id) => !produced.includes(id));
    const sentences = allSentences(summary).filter((s) => s.derived_from.some((r) => reached.has(r.split(":")[0]))).length;
    return { produced, downstream, sentences };
  }, [items, summary, agentId]);

  // Esc closes; focus returns to where it was. onClose is read through a ref so
  // re-renders from polling don't re-run this.
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    const k = (e: KeyboardEvent) => e.key === "Escape" && closeRef.current();
    window.addEventListener("keydown", k);
    return () => {
      window.removeEventListener("keydown", k);
      prev?.focus?.();
    };
  }, []);

  if (!agent) return null;

  const start = async () => {
    setBusy(true);
    setErr(null);
    try {
      const next = await agentApi.rerun(runId, agentId, { instructions: text.trim(), recheck });
      useRunStore.setState((s) => ({ agents: { ...s.agents, [next.agent_id]: next } }));
      onClose();
      useWindows.getState().open("agent", next.agent_id);
    } catch (e) {
      if (e instanceof NotSupported) setUnsupported(true);
      else setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const list = (ids: string[]) => (ids.length > 6 ? `${ids.slice(0, 6).join(", ")} and ${ids.length - 6} more` : ids.join(", "));
  // Portalled to <body>: opened from a window's title bar, it would otherwise be trapped
  // in that window's stacking context and draw underneath it.
  return createPortal(
    <div className="fixed inset-0 z-[200000] flex items-start justify-center bg-black/40 px-4 pt-[14vh] print:hidden" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="agent-rerun-title"
        onMouseDown={(e) => e.stopPropagation()}
        className="w-full max-w-[560px] animate-[snap-in_160ms_ease-out] border border-desk-line-strong bg-desk p-4 text-[13px] text-desk-ink"
      >
        <h2 id="agent-rerun-title" className="text-[15px] font-semibold text-white">
          Rerun <span className="font-mono">{agent.agent_id}</span>
        </h2>
        <p className="mt-1 text-desk-muted">{agent.task}</p>
        <dl className="mt-3 grid gap-1 text-[12px]">
          {agent.target_ids.length > 0 && (
            <div className="flex gap-2">
              <dt className="w-24 flex-none text-desk-muted">Worked on</dt>
              <dd className="font-mono">{list(agent.target_ids)}</dd>
            </div>
          )}
          <div className="flex gap-2">
            <dt className="w-24 flex-none text-desk-muted">Produced</dt>
            <dd className="font-mono">{produced.length ? list(produced) : "No report items"}</dd>
          </div>
          <div className="flex gap-2">
            <dt className="w-24 flex-none text-desk-muted">Depends on it</dt>
            <dd>
              {downstream.length} item{downstream.length === 1 ? "" : "s"} and {sentences} final report sentence{sentences === 1 ? "" : "s"}
            </dd>
          </div>
        </dl>
        <label htmlFor="agent-rerun-text" className="mt-3 block text-[12px] font-medium">
          Instructions for the new {agent.role}
        </label>
        <textarea
          id="agent-rerun-text"
          autoFocus
          rows={3}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && text.trim()) void start();
          }}
          placeholder={EXAMPLE[agent.role]}
          className="mt-1 w-full resize-y rounded-[4px] border border-desk-line-strong bg-desk-raised px-2 py-1.5 text-desk-ink placeholder:text-desk-muted focus:border-accent"
        />
        <label className="mt-2 flex items-center gap-2">
          <input type="checkbox" checked={recheck} onChange={(e) => setRecheck(e.target.checked)} />
          Re-check what depends on it
        </label>
        <p className="mt-2 text-[12px] text-desk-muted">Same run, same gateway rules. The old outputs stay in each item&apos;s history.</p>
        {unsupported && (
          <div role="status" className="mt-2 rounded-[4px] border border-desk-warn/50 px-2.5 py-2 text-desk-warn">
            Rerunning an agent isn&apos;t available yet (ERR backend issue).
          </div>
        )}
        {err && (
          <div role="alert" className="mt-2 text-desk-bad">
            {err}
          </div>
        )}
        <div className="mt-3 flex items-center gap-2">
          <button onClick={() => void start()} disabled={busy || !text.trim() || unsupported} className="h-8 rounded-[4px] bg-accent px-3.5 font-semibold text-accent-ink disabled:opacity-40">
            Rerun agent
          </button>
          <button onClick={onClose} className="h-8 rounded-[4px] px-2.5 text-desk-muted hover:text-white">
            Cancel
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
