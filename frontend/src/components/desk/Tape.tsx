"use client";

import { useMemo, useState } from "react";
import type { RunEvent } from "@/lib/api";
import { BlockedActions } from "./Actions";

const TAG: Record<string, { tag: string; cls: string }> = {
  blocked: { tag: "BLK", cls: "bg-desk-bad text-desk" },
  stop: { tag: "STOP", cls: "bg-desk-bad text-desk" },
  model_call: { tag: "CALL", cls: "text-[#6CB6FF]" },
  fetch: { tag: "GET", cls: "text-desk-ink" },
  check: { tag: "CHK", cls: "text-desk-good" },
  source_change: { tag: "SRC", cls: "text-desk-ink" },
  approval: { tag: "USR", cls: "text-amber" },
  resolution: { tag: "USR", cls: "text-amber" },
  step: { tag: "STEP", cls: "text-desk-muted" },
};

const time = (iso: string) => new Date(iso).toLocaleTimeString("en-GB", { hour12: false });

export function Tape({ events, live, runId, refresh }: { events: RunEvent[]; live: boolean; runId: string; refresh: () => Promise<void> }) {
  const [filter, setFilter] = useState<"all" | "calls" | "blocked">("all");
  const denied = events.filter((e) => e.type === "blocked");
  const calls = events.filter((e) => e.type === "model_call" || e.type === "fetch");
  const tokens = events.reduce((n, e) => n + (e.tokens ?? 0), 0);

  // Events per 5 s bucket over the last 13 buckets, with blocked buckets marked.
  const spark = useMemo(() => {
    if (!events.length) return { line: "", blocks: [] as number[] };
    const end = new Date(events[events.length - 1].ts).getTime();
    const n = 13;
    const counts = new Array(n).fill(0);
    const blk = new Array(n).fill(false);
    for (const e of events) {
      const b = n - 1 - Math.floor((end - new Date(e.ts).getTime()) / 5000);
      if (b >= 0) {
        counts[b]++;
        if (e.decision === "deny") blk[b] = true;
      }
    }
    const max = Math.max(4, ...counts);
    const x = (i: number) => (i * 290) / (n - 1) + 5;
    return {
      line: counts.map((c, i) => `${x(i)},${42 - (c / max) * 38}`).join(" "),
      blocks: blk.map((b, i) => (b ? x(i) : -1)).filter((v) => v >= 0),
    };
  }, [events]);

  const shown = events
    .filter((e) => (filter === "all" ? e.type !== "step" : filter === "blocked" ? e.type === "blocked" : e.type === "model_call" || e.type === "fetch"))
    .slice(-200)
    .reverse();

  return (
    <aside aria-label="Gateway tape" className="flex max-h-[calc(100vh-130px)] min-w-0 flex-[1_1_340px] flex-col bg-desk font-mono text-xs text-desk-ink lg:sticky lg:top-[100px] lg:max-w-[420px]">
      <div className="flex items-center justify-between border-b border-desk-line px-3.5 py-2.5">
        <span className="font-bold tracking-[0.14em]">GATEWAY TAPE</span>
        <span className={live ? "text-desk-good" : "text-desk-muted"}>● {live ? "LIVE" : "IDLE"}</span>
      </div>
      <div className="grid grid-cols-4 border-b border-desk-line">
        {[
          ["EVENTS", events.length, false],
          ["CALLS", calls.length, false],
          ["TOKENS", tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}k` : tokens, false],
          ["BLOCKED", denied.length, true],
        ].map(([label, v, bad]) => (
          <div key={label as string} className="px-3.5 py-2">
            <div className="text-[10px] tracking-[0.12em] text-desk-muted">{label}</div>
            <div className={`text-lg font-medium tabular-nums ${bad && (v as number) > 0 ? "text-desk-bad" : ""}`}>{v}</div>
          </div>
        ))}
      </div>
      <div className="border-b border-desk-line px-3.5 pt-2.5 pb-1.5">
        <svg role="img" aria-label="Gateway events per 5 seconds" viewBox="0 0 300 44" preserveAspectRatio="none" className="h-11 w-full">
          {spark.blocks.map((x) => (
            <line key={x} x1={x} y1={2} x2={x} y2={44} stroke="#FF5A5A" strokeWidth={2} />
          ))}
          <polyline points={spark.line} fill="none" stroke="#E6E6E6" strokeWidth={1.5} />
        </svg>
        <div className="flex justify-between text-[10px] text-desk-muted">
          <span>EVENTS / 5s</span>
          <span className="text-desk-bad">▍BLOCK</span>
        </div>
      </div>
      <div className="flex gap-0.5 border-b border-desk-line px-3.5 py-2 text-[11px]">
        {(["all", "calls", "blocked"] as const).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            aria-pressed={filter === f}
            className={`border px-2 py-0.5 ${filter === f ? "border-desk-ink bg-desk-ink text-desk" : "border-[#555] text-desk-ink"} ${f === "blocked" && filter !== f ? "text-desk-bad" : ""}`}
          >
            {f === "blocked" ? `BLOCKED ${denied.length}` : f.toUpperCase()}
          </button>
        ))}
      </div>
      <ol className="min-h-0 flex-1 overflow-y-auto">
        {shown.length === 0 && <li className="px-3.5 py-3 text-desk-muted">Waiting for events…</li>}
        {shown.map((e) => {
          const t = TAG[e.type];
          return (
            <li key={e.seq} className={`flex animate-[tape-in_240ms_ease-out] gap-2.5 border-b border-[#1C1C1C] px-3.5 py-1.5 ${e.decision === "deny" ? "bg-[#2A0E0E]" : ""}`}>
              <span className="text-desk-muted tabular-nums">{time(e.ts)}</span>
              <span className={`w-9 flex-none text-center font-bold ${t.cls}`}>{t.tag}</span>
              <span className="min-w-0">
                <span className="text-desk-muted">{e.actor.id} </span>
                {e.summary}
                {(e.latency_ms || e.tokens) && (
                  <span className="text-desk-muted">
                    {" "}
                    {e.latency_ms ? `${e.latency_ms}ms` : ""} {e.tokens ? `${e.tokens.toLocaleString()}tok` : ""}
                  </span>
                )}
                {e.type === "blocked" && !e.claim_id && <BlockedActions dark runId={runId} event={e} events={events} refresh={refresh} />}
              </span>
            </li>
          );
        })}
      </ol>
    </aside>
  );
}
