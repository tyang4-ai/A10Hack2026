"use client";

import { useEffect, useState } from "react";
import type { RunDetail, RunStatus } from "@/lib/api";

export type Tab = "sources" | "verify" | "claims" | "report";

const STATUS: Record<RunStatus, { label: string; cls: string }> = {
  running: { label: "RUNNING", cls: "text-[#6CB6FF]" },
  awaiting_approval: { label: "AWAITING APPROVAL", cls: "text-amber" },
  completed: { label: "COMPLETE", cls: "text-desk-good" },
  stopped: { label: "STOPPED", cls: "text-desk-bad" },
  failed: { label: "FAILED", cls: "text-desk-bad" },
};

function Clock() {
  const [now, setNow] = useState("");
  useEffect(() => {
    const tick = () => setNow(new Date().toLocaleTimeString("en-GB", { hour12: false }));
    tick();
    const t = setInterval(tick, 1000);
    return () => clearInterval(t);
  }, []);
  return <span className="text-[#A8A8A8] tabular-nums">{now}</span>;
}

export function TopBar({
  run,
  tab,
  onTab,
  counts,
  onStop,
}: {
  run: RunDetail | null;
  tab: Tab;
  onTab: (t: Tab) => void;
  counts: Partial<Record<Tab, { n: number; bad?: boolean }>>;
  onStop: () => void;
}) {
  const status = run ? STATUS[run.status] : null;
  const live = run && (run.status === "running" || run.status === "awaiting_approval");
  const tabs: [Tab, string][] = [["sources", "SOURCES"], ["verify", "VERIFY"], ["claims", "CLAIMS"], ["report", "REPORT"]];
  return (
    <div className="bg-desk font-mono text-xs text-[#F2F2F2]">
      <div className="mx-auto flex min-h-12 max-w-[1280px] flex-wrap items-center gap-x-[22px] gap-y-1.5 px-6">
        <span className="text-sm font-bold tracking-[0.2em] text-amber">AXELROD</span>
        {run && (
          <span className="text-[#A8A8A8]">
            RUNS ▸ <span className="text-[#F2F2F2]">{run.company.toUpperCase()}</span> ▸ {run.id}
          </span>
        )}
        {run && (
          <nav aria-label="Run sections" className="flex gap-[18px] self-stretch">
            {tabs.map(([id, label]) => {
              const on = tab === id;
              const c = counts[id];
              return (
                <button
                  key={id}
                  aria-current={on ? "page" : undefined}
                  onClick={() => onTab(id)}
                  className={`flex items-center border-b-2 pt-0.5 transition-colors ${on ? "border-amber font-bold text-white" : "border-transparent text-[#A8A8A8] hover:text-white"}`}
                >
                  {label}
                  {c && c.n > 0 && <span className={`ml-1.5 ${c.bad ? "text-desk-bad" : "text-[#A8A8A8]"}`}>{c.n}</span>}
                </button>
              );
            })}
          </nav>
        )}
        <div className="ml-auto flex flex-wrap items-center gap-4">
          <Clock />
          {status && run && (
            <span className={status.cls}>
              ● {status.label}
              {run.status === "running" && ` ${String(run.current_step).padStart(2, "0")}/12`}
            </span>
          )}
          {live && (
            <button
              onClick={onStop}
              className="min-h-8 border border-desk-bad px-3 font-bold tracking-[0.08em] text-desk-bad transition-colors hover:bg-desk-bad hover:text-desk"
            >
              ■ STOP RUN <kbd className="ml-1.5 border border-current px-1 font-normal">⇧S</kbd>
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

const TICKER: [string, string, string, boolean][] = [
  ["SHL GY", "52.10", "+1.6%", true],
  ["DAX", "21,840", "-0.3%", false],
  ["SXDP", "1,024", "+0.4%", true],
  ["EURUSD", "1.0842", "-0.1%", false],
  ["BUND 10Y", "2.41%", "+2bp", true],
];

export function Ticker() {
  return (
    <div className="overflow-x-auto border-b border-ink bg-[#F3F3F3] font-mono text-xs">
      <div className="mx-auto flex max-w-[1280px] gap-7 whitespace-nowrap px-6 py-1.5">
        {TICKER.map(([sym, px, chg, up]) => (
          <span key={sym}>
            <strong>{sym}</strong> {px} <span className={up ? "text-good" : "text-bad"}>{up ? "▲" : "▼"} {chg}</span>
          </span>
        ))}
        <span className="ml-auto text-muted">MOCK FEED</span>
      </div>
    </div>
  );
}

const SHORT = ["Company", "Data types", "Propose", "Approve", "Fetch", "Draft", "Quotes", "Validate", "Scenarios", "Write", "Summary", "Display"];

export function Progress({ run }: { run: RunDetail }) {
  return (
    <div role="img" aria-label={`Pipeline at step ${run.current_step} of 12`} className="grid grid-cols-12 gap-0.5 font-mono text-[10px]">
      {run.steps.map((s, i) => {
        const done = s.status === "done";
        const now = s.status === "running" || s.status === "waiting";
        const bad = s.status === "failed" || s.status === "stopped";
        return (
          <div
            key={s.n}
            className={`px-1.5 py-1 transition-colors duration-500 ${
              done ? "bg-ink text-white" : now ? "bg-white text-ink outline-2 -outline-offset-2 outline-ink" : bad ? "bg-bad text-white" : "bg-[#EDEDED] text-muted"
            }`}
          >
            <div className="font-bold">
              {String(s.n).padStart(2, "0")}
              {done ? " ✓" : now ? " ▶" : bad ? " ■" : ""}
            </div>
            <div className="truncate">{SHORT[i].toUpperCase()}</div>
          </div>
        );
      })}
    </div>
  );
}

export function PageHead({ over, title, run, children }: { over: string; title: string; run: RunDetail; children?: React.ReactNode }) {
  const started = new Date(run.created_at).toLocaleString("en-GB", { day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit" });
  return (
    <div>
      <div className="font-mono text-[11px] tracking-[0.12em] text-muted">{over}</div>
      <div className="mt-0.5 flex flex-wrap items-baseline gap-x-4 gap-y-0.5">
        <h1 className="text-[26px] font-normal">{title}</h1>
        <span className="text-sm">
          {run.company}
          <sup className="text-[10px]">†</sup> · started by {run.started_by} · {started}
        </span>
      </div>
      {children && <p className="mt-2 max-w-[820px] text-justify text-[15px]">{children}</p>}
    </div>
  );
}

export function Section({ n, title, children }: { n: string; title: string; children: React.ReactNode }) {
  return (
    <section className="mt-6">
      <h2 className="mb-2 text-lg font-bold">
        <span className="mr-2.5 font-mono text-[13px]">{n}</span>
        {title}
      </h2>
      {children}
    </section>
  );
}

export function Footnote() {
  return <p className="mt-7 max-w-60 border-t border-ink pt-1.5 text-xs"><sup>†</sup>All figures and prices are mock data.</p>;
}

export function StatRow({ stats }: { stats: { label: string; value: string | number; bad?: boolean }[] }) {
  return (
    <div className="grid border-y border-ink" style={{ gridTemplateColumns: `repeat(${stats.length}, minmax(0, 1fr))` }}>
      {stats.map((s) => (
        <div key={s.label} className="border-r border-rule px-3 py-2 last:border-r-0">
          <div className="font-mono text-[10px] tracking-[0.12em] text-muted">{s.label}</div>
          <div className={`font-mono text-2xl font-medium tabular-nums transition-colors ${s.bad ? "text-bad" : ""}`}>{s.value}</div>
        </div>
      ))}
    </div>
  );
}

export const btn = "border border-ink px-2.5 font-mono text-[11px] tracking-[0.06em] transition-colors hover:bg-ink hover:text-white disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-ink";
