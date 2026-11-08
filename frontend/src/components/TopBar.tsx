"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { STAGES, STAGE_LABEL, usd } from "@/lib/format";
import { useRunStore } from "@/lib/store";

const STATUS_CLS = {
  running: "text-[#6CB6FF]",
  awaiting_approval: "text-amber",
  done: "text-desk-good",
  stopped: "text-desk-bad",
  failed: "text-desk-bad",
} as const;

function StopButton() {
  const run = useRunStore((s) => s.run);
  const refreshRun = useRunStore((s) => s.refreshRun);
  const [confirm, setConfirm] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    if (!confirm) return;
    const t = setTimeout(() => setConfirm(false), 5000);
    return () => clearTimeout(t);
  }, [confirm]);
  if (!run || run.status === "done" || run.status === "stopped" || run.status === "failed") return null;
  const stop = async () => {
    try {
      await api.stop(run.run_id, "Stopped by the analyst from the dashboard");
      setConfirm(false);
      await refreshRun();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    }
  };
  return (
    <div className="flex items-center gap-1.5">
      {confirm ? (
        <>
          <button onClick={stop} className="min-h-9 bg-desk-bad px-4 font-bold tracking-[0.08em] text-desk">
            ■ CONFIRM STOP
          </button>
          <button onClick={() => setConfirm(false)} className="min-h-9 border border-[#555] px-2.5 text-desk-ink">
            CANCEL
          </button>
        </>
      ) : (
        <button onClick={() => setConfirm(true)} className="min-h-9 border-2 border-desk-bad px-4 font-bold tracking-[0.1em] text-desk-bad transition-colors hover:bg-desk-bad hover:text-desk">
          ■ STOP
        </button>
      )}
      {err && <span className="text-desk-bad">{err}</span>}
    </div>
  );
}

function NewRun() {
  const [open, setOpen] = useState(false);
  const [company, setCompany] = useState("Siemens Healthineers");
  const [busy, setBusy] = useState(false);
  const { openRun, loadRuns } = useRunStore.getState();
  if (!open)
    return (
      <button onClick={() => setOpen(true)} className="border border-[#555] px-2 py-1 text-desk-ink hover:border-desk-ink">
        + NEW RUN
      </button>
    );
  return (
    <form
      className="flex items-center gap-1.5"
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        try {
          const run = await api.createRun({ company, created_via: "ui" });
          await loadRuns();
          await openRun(run.run_id);
          setOpen(false);
        } finally {
          setBusy(false);
        }
      }}
    >
      <input
        aria-label="Company"
        autoFocus
        value={company}
        onChange={(e) => setCompany(e.target.value)}
        className="w-48 border-b border-[#555] bg-transparent py-0.5 text-desk-ink outline-none focus:border-amber"
      />
      <button disabled={busy || !company.trim()} className="border border-amber px-2 py-1 font-bold text-amber disabled:opacity-40">
        START
      </button>
      <button type="button" onClick={() => setOpen(false)} className="px-1 text-desk-muted">
        ✕
      </button>
    </form>
  );
}

export function TopBar() {
  const runs = useRunStore((s) => s.runs);
  const run = useRunStore((s) => s.run);
  const runId = useRunStore((s) => s.runId);
  const openRun = useRunStore((s) => s.openRun);
  const stageIdx = run ? STAGES.indexOf(run.stage) : -1;
  const c = run?.counts;

  return (
    <header className="flex-none bg-desk font-mono text-xs text-desk-ink">
      <div className="flex min-h-12 flex-wrap items-center gap-x-5 gap-y-1.5 px-4">
        <span className="text-sm font-bold tracking-[0.2em] text-amber">AXELROD</span>
        <select
          aria-label="Run"
          value={runId ?? ""}
          onChange={(e) => openRun(e.target.value || null)}
          className="max-w-64 border border-[#555] bg-desk px-1.5 py-1 text-desk-ink"
        >
          <option value="">Select a run…</option>
          {runs.map((r) => (
            <option key={r.run_id} value={r.run_id}>
              {r.company} · {r.run_id} · {r.status}
            </option>
          ))}
        </select>
        <NewRun />
        {run && (
          <>
            <span className="text-sm text-white">
              {run.company}
              {run.ticker && <span className="ml-1.5 text-desk-muted">{run.ticker}</span>}
            </span>
            {run.mode !== "live" && (
              <span title="Simulated data, not a live run" className="bg-amber px-1.5 font-bold tracking-[0.1em] text-desk">
                {run.mode.toUpperCase()}
              </span>
            )}
            <span className={STATUS_CLS[run.status]}>● {run.status.replace("_", " ").toUpperCase()}</span>
          </>
        )}
        <div className="ml-auto flex flex-wrap items-center gap-4">
          {run && (
            <>
              <span title="AWS spend for this run, from the gateway meter">
                <span className="text-desk-muted">COST </span>
                <span className="tabular-nums text-white">{usd(run.usage.cost_usd)}</span>
                {run.usage.budget_usd !== null && <span className="text-desk-muted"> / {usd(run.usage.budget_usd)}</span>}
              </span>
              {c && (
                <span className="text-desk-muted">
                  ITEMS <span className="text-white tabular-nums">{c.items_total}</span> · AGENTS{" "}
                  <span className="text-white tabular-nums">{c.agents_active}/{c.agents_total}</span> · BLOCKED{" "}
                  <span className={`tabular-nums ${c.gateway_blocked ? "text-desk-bad" : "text-white"}`}>{c.gateway_blocked}</span>
                  {c.gateway_flagged > 0 && (
                    <>
                      {" "}
                      · FLAGGED <span className="tabular-nums text-amber">{c.gateway_flagged}</span>
                    </>
                  )}
                </span>
              )}
            </>
          )}
          <StopButton />
        </div>
      </div>
      {run && (
        <ol aria-label="Run stage" className="grid grid-cols-10 gap-px border-t border-desk-line bg-desk-line text-[10px]">
          {STAGES.map((s, i) => {
            const done = i < stageIdx || run.stage === "complete";
            const now = i === stageIdx && run.stage !== "complete";
            const halted = now && (run.status === "stopped" || run.status === "failed");
            return (
              <li
                key={s}
                aria-current={now ? "step" : undefined}
                className={`truncate px-2 py-1 transition-colors duration-500 ${
                  halted ? "bg-desk-bad text-desk" : now ? (run.status === "awaiting_approval" ? "bg-amber text-desk" : "bg-white text-desk") : done ? "bg-[#1C1C1C] text-desk-good" : "bg-desk text-desk-muted"
                }`}
              >
                {done ? "✓ " : now ? "▶ " : ""}
                {STAGE_LABEL[s]}
              </li>
            );
          })}
        </ol>
      )}
    </header>
  );
}

export function Banner() {
  const run = useRunStore((s) => s.run);
  const traffic = useRunStore((s) => s.traffic);
  if (!run) return null;
  if (run.status === "stopped") {
    const after = traffic.filter((t) => t.data.decision === "blocked" && t.data.rule_id === null && /kill|stop/i.test(t.data.reason)).length;
    return (
      <div role="alert" className="flex-none bg-desk-bad px-4 py-2 font-mono text-xs font-bold tracking-[0.06em] text-desk">
        ■ KILL SWITCH ON · run stopped{run.stop_reason ? `: ${run.stop_reason}` : ""}. The gateway now denies every request
        {after > 0 ? ` (${after} blocked since)` : ""}.
      </div>
    );
  }
  if (run.status === "failed")
    return (
      <div role="alert" className="flex-none bg-[#5A0000] px-4 py-2 font-mono text-xs text-white">
        RUN FAILED · {run.error ?? "Backend error"}
      </div>
    );
  return null;
}
