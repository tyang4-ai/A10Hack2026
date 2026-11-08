"use client";

// The home page: every run, grouped by company. Each company is a project and
// each run is one session of the harness on it. Click a run to open its desk.
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import type { Run } from "@contract/types";
import { api } from "@/lib/api";
import { STAGE_LABEL, usd } from "@/lib/format";
import { ago, STATUS_DOT, STATUS_WORD, ThemeSwitcher } from "./Switchers";
import { NewRun } from "./TopBar";
import { Skeleton } from "./ui";

// Fixed widths so every company's table lines up with the others.
const COLUMNS: [string, string][] = [
  ["Run", "w-[27%]"],
  ["Status", "w-[20%]"],
  ["Stage", "w-[12%]"],
  ["Items checked", "w-[16%]"],
  ["Blocked", "w-[8%]"],
  ["Cost", "w-[8%]"],
  ["Updated", "w-[9%]"],
];

const LIVE = new Set<Run["status"]>(["running", "awaiting_approval"]);

export function Home() {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Refresh every 2 s while a run is live, so status and stage stay current; every 10 s otherwise.
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      let live = false;
      try {
        const r = await api.listRuns();
        if (!alive) return;
        setRuns(r.runs);
        setErr(null);
        live = r.runs.some((x) => LIVE.has(x.status));
      } catch {
        if (alive) setErr("Cannot reach the API. Is the mock running on port 8000?");
      }
      if (alive) timer = setTimeout(tick, live ? 2000 : 10000);
    };
    void tick();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, []);

  const projects = useMemo(() => {
    const by = new Map<string, Run[]>();
    for (const r of [...(runs ?? [])].sort((a, b) => b.updated_at.localeCompare(a.updated_at))) by.set(r.company, [...(by.get(r.company) ?? []), r]);
    return [...by.entries()].map(([company, list]) => ({ company, ticker: list.find((r) => r.ticker)?.ticker ?? null, runs: list }));
  }, [runs]);

  const live = runs?.filter((r) => LIVE.has(r.status)).length ?? 0;
  const spend = runs?.reduce((s, r) => s + r.usage.cost_usd, 0) ?? 0;

  return (
    <div className="min-h-screen bg-paper">
      <header className="bg-desk text-[13px] text-desk-ink">
        <div className="mx-auto flex min-h-12 max-w-6xl flex-wrap items-center gap-3 px-4 py-2 sm:px-6">
          <span className="font-mono text-sm font-semibold tracking-[0.18em] text-accent">AXELROD</span>
          <span className="text-desk-muted">Self-verifying research harness</span>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <ThemeSwitcher />
            <NewRun startOpen />
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6">
        <h1 className="text-[22px] font-semibold text-ink">Runs</h1>
        <p className="mt-1 max-w-[72ch] text-[14px] text-muted">Every run of the harness, grouped by company. Open one to see its sources, agents, gateway traffic, report and brief.</p>

        {runs && (
          <dl className="mt-4 flex flex-wrap gap-x-6 gap-y-1 text-[13px]">
            <div className="flex gap-1.5">
              <dt className="text-muted">Companies</dt>
              <dd className="font-mono tabular-nums">{projects.length}</dd>
            </div>
            <div className="flex gap-1.5">
              <dt className="text-muted">Runs</dt>
              <dd className="font-mono tabular-nums">{runs.length}</dd>
            </div>
            <div className="flex gap-1.5">
              <dt className="text-muted">Live now</dt>
              <dd className="font-mono tabular-nums">{live}</dd>
            </div>
            <div className="flex gap-1.5" title="AWS spend across all runs, from the gateway meter">
              <dt className="text-muted">Total cost</dt>
              <dd className="font-mono tabular-nums">{usd(spend)}</dd>
            </div>
          </dl>
        )}

        {err && (
          <p role="alert" className="mt-6 text-bad">
            {err}
          </p>
        )}
        {!runs && !err && (
          <div className="mt-6">
            <Skeleton rows={6} />
          </div>
        )}
        {runs && runs.length === 0 && <p className="mt-6 text-muted">No runs yet. Start one with New run above.</p>}

        {projects.map((p) => (
          <section key={p.company} aria-labelledby={`p-${p.company}`} className="mt-8">
            <div className="mb-2 flex flex-wrap items-baseline gap-x-3">
              <h2 id={`p-${p.company}`} className="text-[15px] font-semibold text-ink">
                {p.company}
              </h2>
              {p.ticker && <span className="font-mono text-[12px] text-muted">{p.ticker}</span>}
              <span className="text-[12px] text-muted">
                {p.runs.length} run{p.runs.length > 1 ? "s" : ""}
              </span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] table-fixed border-collapse border-t border-ink text-[13px]">
                <thead className="text-left text-[12px] text-muted">
                  <tr className="border-b border-rule">
                    {COLUMNS.map(([h, w], i) => (
                      <th key={h} className={`px-2 py-1.5 font-medium ${w} ${i >= 3 && i <= 5 ? "text-right" : ""}`}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {p.runs.map((r) => (
                    <RunRow key={r.run_id} r={r} />
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        ))}
      </main>
    </div>
  );
}

function RunRow({ r }: { r: Run }) {
  const c = r.counts;
  const checked = c.items_pass + c.items_fixed;
  const href = `/runs/${encodeURIComponent(r.run_id)}`;
  return (
    <tr className="border-b border-rule hover:bg-zebra">
      <td className="px-2 py-2">
        <Link href={href} className="flex items-center gap-2 font-mono text-link hover:underline" title={`Open ${r.run_id}`}>
          {r.run_id}
        </Link>
        {r.mode !== "live" && (
          <span title="Simulated data, not a live run" className="mt-0.5 inline-block rounded-[3px] bg-warn-bg px-1 font-mono text-[10px] font-semibold text-warn">
            {r.mode.toUpperCase()}
          </span>
        )}
      </td>
      <td className="px-2 py-2 whitespace-nowrap">
        <span className="flex items-center gap-1.5">
          <span aria-hidden className={`h-2 w-2 rounded-full ${STATUS_DOT[r.status]} ${r.status === "running" ? "animate-pulse" : ""}`} />
          {STATUS_WORD[r.status]}
        </span>
        {r.stop_reason && <div className="max-w-56 truncate text-[12px] text-muted" title={r.stop_reason}>{r.stop_reason}</div>}
        {r.error && <div className="max-w-56 truncate text-[12px] text-bad" title={r.error}>{r.error}</div>}
      </td>
      <td className="px-2 py-2 whitespace-nowrap">{STAGE_LABEL[r.stage]}</td>
      <td className="px-2 py-2 text-right font-mono whitespace-nowrap tabular-nums">
        {checked}
        <span className="text-muted"> of {c.items_total}</span>
        {c.items_fail > 0 && <span className="text-bad"> ({c.items_fail} fail)</span>}
      </td>
      <td className={`px-2 py-2 text-right font-mono tabular-nums ${c.gateway_blocked ? "text-bad" : ""}`}>{c.gateway_blocked}</td>
      <td className="px-2 py-2 text-right font-mono tabular-nums">{usd(r.usage.cost_usd)}</td>
      <td className="px-2 py-2 whitespace-nowrap text-muted" title={new Date(r.updated_at).toLocaleString("en-GB")}>
        {ago(r.updated_at)}
      </td>
    </tr>
  );
}
