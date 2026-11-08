"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import type { Stage } from "@contract/types";
import { api, ApiError } from "@/lib/api";
import { MODE_HELP, MODE_LABEL, STAGES, STAGE_LABEL, usd } from "@/lib/format";
import { useRunStore } from "@/lib/store";
import { LayoutControl } from "./Desk";
import { RerunDialog } from "./RerunDialog";
import { RunSwitcher, ThemeSwitcher } from "./Switchers";

const STATUS = {
  running: { label: "Running", dot: "bg-desk-link" },
  awaiting_approval: { label: "Waiting for your approval", dot: "bg-accent" },
  done: { label: "Done", dot: "bg-desk-good" },
  stopped: { label: "Stopped", dot: "bg-desk-bad" },
  failed: { label: "Failed", dot: "bg-desk-bad" },
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
          <button onClick={stop} className="h-8 rounded-[4px] bg-desk-bad px-3.5 text-[13px] font-semibold text-desk">
            Confirm stop
          </button>
          <button onClick={() => setConfirm(false)} className="h-8 rounded-[4px] px-2.5 text-[13px] text-desk-muted hover:text-white">
            Cancel
          </button>
        </>
      ) : (
        <button
          onClick={() => setConfirm(true)}
          title="Blocks every agent request. Can't be undone."
          className="flex h-8 items-center gap-2 rounded-[4px] border border-desk-bad px-3.5 text-[13px] font-semibold text-desk-bad transition-colors hover:bg-desk-bad hover:text-desk"
        >
          <span aria-hidden className="h-2.5 w-2.5 bg-current" />
          Stop run
        </button>
      )}
      {err && (
        <span role="alert" className="text-desk-bad">
          {err}
        </span>
      )}
    </div>
  );
}

// Starts a run and goes to its desk. Also used on the home page.
export function NewRun({ startOpen = false }: { startOpen?: boolean }) {
  const [open, setOpen] = useState(startOpen);
  const [company, setCompany] = useState("Siemens Healthineers");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const router = useRouter();
  if (!open)
    return (
      <button onClick={() => setOpen(true)} className="h-8 rounded-[4px] border border-desk-line px-2.5 text-desk-ink hover:border-desk-muted">
        New run
      </button>
    );
  return (
    <form
      className="flex items-center gap-1.5"
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setErr(null);
        try {
          const run = await api.createRun({ company, created_via: "ui" });
          router.push(`/runs/${encodeURIComponent(run.run_id)}`);
          setOpen(startOpen);
        } catch (x) {
          setErr(x instanceof Error ? x.message : String(x));
        } finally {
          setBusy(false);
        }
      }}
    >
      <label className="sr-only" htmlFor="new-company">
        Company to research
      </label>
      <input
        id="new-company"
        autoFocus={!startOpen}
        value={company}
        onChange={(e) => setCompany(e.target.value)}
        className="h-8 w-52 rounded-[4px] border border-desk-line bg-desk-raised px-2 text-desk-ink focus:border-accent"
      />
      <button disabled={busy || !company.trim()} className="h-8 rounded-[4px] bg-accent px-3 font-semibold text-accent-ink disabled:opacity-40">
        Start
      </button>
      {!startOpen && (
        <button type="button" onClick={() => setOpen(false)} className="h-8 px-1.5 text-desk-muted hover:text-white" aria-label="Cancel new run">
          ✕
        </button>
      )}
      {err && (
        <span role="alert" className="text-desk-bad">
          {err}
        </span>
      )}
    </form>
  );
}

export function TopBar() {
  const run = useRunStore((s) => s.run);
  const [rerun, setRerun] = useState<Stage | null>(null);
  const stageIdx = run ? STAGES.indexOf(run.stage) : -1;
  const c = run?.counts;
  const st = run ? STATUS[run.status] : null;

  return (
    <header className="flex-none bg-desk text-[13px] text-desk-ink">
      <div className="flex min-h-12 flex-wrap items-center gap-x-3 gap-y-1.5 px-4">
        <Link href="/" title="All runs" aria-label="Axelrod, all runs" className="mr-1 rounded-[4px] font-logo text-[20px] leading-none font-semibold tracking-[0.02em] text-accent hover:text-white">
          Axelrod
        </Link>
        <RunSwitcher />
        {run && st && (
          <div className="flex items-center gap-3">
            {run.mode !== "live" && (
              <span title={MODE_HELP[run.mode]} className="rounded-[3px] bg-desk-warn px-1.5 font-mono text-[11px] font-semibold text-desk">
                {MODE_LABEL}
              </span>
            )}
            {/* Done needs no label: every step in the stage bar is ticked. */}
            {run.status !== "done" && (
              <span className="flex items-center gap-1.5">
                <span className={`h-2 w-2 rounded-full ${st.dot} ${run.status === "running" ? "animate-pulse" : ""}`} />
                {st.label}
              </span>
            )}
          </div>
        )}
        <LayoutControl />
        <div className="ml-auto flex flex-wrap items-center gap-5">
          {run && c && (
            <dl className="flex items-center gap-4 text-desk-muted">
              <div className="flex gap-1.5" title="Metered at the gateway">
                <dt>Cost</dt>
                <dd className="font-mono text-white tabular-nums">
                  {usd(run.usage.cost_usd)}
                  {run.usage.budget_usd !== null && <span className="text-desk-muted"> of {usd(run.usage.budget_usd)}</span>}
                </dd>
              </div>
              <div className="flex gap-1.5">
                <dt>Items</dt>
                <dd className="font-mono text-white tabular-nums">{c.items_total}</dd>
              </div>
              <div className="flex gap-1.5">
                <dt>Agents</dt>
                <dd className="font-mono text-white tabular-nums">
                  {c.agents_active}/{c.agents_total}
                </dd>
              </div>
              <div className="flex gap-1.5">
                <dt>Blocked</dt>
                <dd className={`font-mono tabular-nums ${c.gateway_blocked ? "text-desk-bad" : "text-white"}`}>{c.gateway_blocked}</dd>
              </div>
              {c.gateway_flagged > 0 && (
                <div className="flex gap-1.5">
                  <dt>Flagged</dt>
                  <dd className="font-mono text-desk-warn tabular-nums">{c.gateway_flagged}</dd>
                </div>
              )}
            </dl>
          )}
          <div className="flex items-center gap-1.5">
            <ThemeSwitcher />
            <NewRun />
          </div>
          <StopButton />
        </div>
      </div>
      {run && (
        <ol aria-label="Run stage" className="grid grid-cols-10 border-t border-desk-line text-[12px]">
          {STAGES.map((s, i) => {
            const done = i < stageIdx || run.stage === "complete";
            const now = i === stageIdx && run.stage !== "complete";
            const halted = now && (run.status === "stopped" || run.status === "failed");
            const waiting = now && run.status === "awaiting_approval";
            // A finished step, or the step a stopped or failed run halted on, can be rerun with new instructions.
            const canRerun = s !== "complete" && (done || halted);
            return (
              <li
                key={s}
                aria-current={now ? "step" : undefined}
                className={`relative truncate border-r border-desk-line px-2.5 py-1.5 last:border-r-0 ${
                  halted ? "bg-desk-bad font-semibold text-desk" : waiting ? "bg-accent font-semibold text-accent-ink" : now ? "font-semibold text-white" : done ? "text-desk-ink" : "text-desk-muted/60"
                }`}
              >
                {canRerun ? (
                  <button
                    onClick={() => setRerun(s)}
                    title="Rerun from here"
                    aria-label={`${STAGE_LABEL[s]}, done. Rerun from here`}
                    className="-mx-1 truncate rounded-[3px] px-1 text-left hover:bg-desk-raised hover:underline"
                  >
                    <span className={`mr-1.5 font-mono text-[10px] ${done ? "text-desk-good" : ""}`}>{done ? "✓" : String(i + 1).padStart(2, "0")}</span>
                    {STAGE_LABEL[s]}
                  </button>
                ) : (
                  <>
                    <span className={`mr-1.5 font-mono text-[10px] ${done ? "text-desk-good" : ""}`}>{done ? "✓" : String(i + 1).padStart(2, "0")}</span>
                    {STAGE_LABEL[s]}
                  </>
                )}
                {now && !halted && !waiting && <span className="absolute inset-x-0 bottom-0 h-0.5 animate-pulse bg-desk-link" />}
              </li>
            );
          })}
        </ol>
      )}
      {run && rerun && <RerunDialog runId={run.run_id} stage={rerun} onClose={() => setRerun(null)} />}
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
      <div role="alert" className="flex flex-none items-center gap-2 bg-desk-bad px-4 py-2 text-[13px] font-semibold text-desk">
        <span aria-hidden className="h-2.5 w-2.5 bg-desk" />
        Run stopped. Every agent request is blocked.{after > 0 ? ` ${after} so far.` : ""}
      </div>
    );
  }
  if (run.status === "failed")
    return (
      <div role="alert" className="flex-none bg-failed-bg px-4 py-2 text-[13px] text-white">
        Run failed. {run.error ?? "No error details were returned."}
      </div>
    );
  return null;
}
