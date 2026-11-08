"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { moduleFor, type Module } from "@/lib/format";
import { useRunStore } from "@/lib/store";
import { usePolling } from "@/lib/usePolling";
import { BriefModule } from "./BriefModule";
import { Drawer } from "./Drawer";
import { Inspector } from "./Inspector";
import { ReportModule } from "./report";
import { SourcesModule } from "./SourcesModule";
import { Banner, TopBar } from "./TopBar";

const MODULES: { id: Module; n: string; label: string; stages: string }[] = [
  { id: "sources", n: "1", label: "Sources", stages: "plan · approve · collect" },
  { id: "report", n: "2", label: "Agent report", stages: "extract · draft · verify · repair" },
  { id: "brief", n: "3", label: "Brief", stages: "summarize · check" },
];

function Rail() {
  const module = useRunStore((s) => s.module);
  const setModule = useRunStore((s) => s.setModule);
  const run = useRunStore((s) => s.run);
  const [note, setNote] = useState<string | null>(null);
  const live = run?.stage ? moduleFor(run.stage) : null;
  return (
    <nav aria-label="Modules" className="flex w-44 flex-none flex-col border-r border-ink font-mono text-xs print:hidden">
      {MODULES.map((m) => (
        <button
          key={m.id}
          aria-current={module === m.id ? "page" : undefined}
          onClick={() => setModule(m.id)}
          className={`border-b border-rule px-3 py-3 text-left transition-colors ${module === m.id ? "bg-ink text-white" : "hover:bg-zebra"}`}
        >
          <div className="flex items-center gap-2">
            <span className="text-[10px] opacity-60">{m.n}</span>
            <span className="font-bold tracking-[0.06em]">{m.label.toUpperCase()}</span>
            {live === m.id && run?.status !== "done" && <span className={`ml-auto h-1.5 w-1.5 rounded-full ${module === m.id ? "bg-amber" : "bg-[#1A6FB0]"} animate-pulse`} />}
          </div>
          <div className={`mt-0.5 text-[10px] ${module === m.id ? "text-[#BDBDBD]" : "text-muted"}`}>{m.stages}</div>
        </button>
      ))}
      {run && run.status !== "stopped" && (
        <div className="mt-auto border-t border-rule p-3">
          <div className="text-[10px] tracking-[0.1em] text-muted">DEMO CONTROL</div>
          <button
            className="mt-1 w-full border border-dashed border-[#8A5A00] px-2 py-1 text-[11px] text-[#8A5A00] hover:bg-[#FFF6DE]"
            title="Corrupts one digit in a few quotes so the checker can catch them. Simulated."
            onClick={async () => {
              try {
                const r = await api.injectErrors(run.run_id, 3);
                setNote(`Injected into ${r.item_ids.join(", ")}`);
              } catch (e) {
                setNote(e instanceof Error ? e.message : String(e));
              }
            }}
          >
            INJECT ERRORS (SIMULATED)
          </button>
          {note && <div className="mt-1 text-[10px] text-muted">{note}</div>}
        </div>
      )}
    </nav>
  );
}

export function Dashboard() {
  const runId = useRunStore((s) => s.runId);
  const run = useRunStore((s) => s.run);
  const module = useRunStore((s) => s.module);
  const loading = useRunStore((s) => s.loading);
  const error = useRunStore((s) => s.error);
  const { loadRuns, openRun, setModule } = useRunStore.getState();
  usePolling(runId);

  // First load: list runs and open the newest.
  useEffect(() => {
    loadRuns().then(() => {
      const runs = useRunStore.getState().runs;
      if (!useRunStore.getState().runId && runs[0]) void openRun(runs[0].run_id);
    }, () => useRunStore.setState({ error: "Cannot reach the API. Is the mock running on port 8000?" }));
  }, [loadRuns, openRun]);

  // Follow the run: switch module when the stage moves to a new module.
  const lastStage = useRef<string | null>(null);
  useEffect(() => {
    if (!run) return;
    if (lastStage.current && lastStage.current !== run.stage && moduleFor(run.stage) !== moduleFor(lastStage.current as never)) setModule(moduleFor(run.stage));
    lastStage.current = run.stage;
  }, [run, setModule]);
  useEffect(() => {
    lastStage.current = null;
  }, [runId]);

  return (
    <div className="flex h-screen flex-col overflow-hidden print:block print:h-auto print:overflow-visible">
      <div className="print:hidden">
        <TopBar />
        <Banner />
      </div>
      <div className="flex min-h-0 flex-1 print:block">
        <Rail />
        <main className="min-w-0 flex-1 overflow-y-auto px-6 py-5 print:overflow-visible print:p-0">
          {error && <p className="font-mono text-xs text-bad">{error}</p>}
          {!runId && !error && <p className="font-mono text-xs text-muted">Pick a run or start a new one.</p>}
          {loading && <p className="font-mono text-xs text-muted">Loading run…</p>}
          {run && !loading && (
            <>
              {module === "sources" && <SourcesModule />}
              {module === "report" && <ReportModule />}
              {module === "brief" && <BriefModule />}
            </>
          )}
        </main>
        <div className="w-[360px] flex-none border-l border-ink print:hidden">{run && <Inspector />}</div>
      </div>
      <div className="print:hidden">{run && <Drawer />}</div>
    </div>
  );
}
