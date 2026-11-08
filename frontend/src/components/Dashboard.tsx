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
import { Skeleton } from "./ui";

const MODULES: { id: Module; n: string; label: string; stages: string }[] = [
  { id: "sources", n: "1", label: "Sources", stages: "Plan, approve, collect" },
  { id: "report", n: "2", label: "Agent report", stages: "Extract, draft, verify, repair" },
  { id: "brief", n: "3", label: "Brief", stages: "Summarize and check" },
];

function Rail() {
  const module = useRunStore((s) => s.module);
  const setModule = useRunStore((s) => s.setModule);
  const run = useRunStore((s) => s.run);
  const [note, setNote] = useState<string | null>(null);
  const live = run?.stage ? moduleFor(run.stage) : null;
  return (
    <nav aria-label="Modules" className="flex w-48 flex-none flex-col gap-0.5 border-r border-rule bg-zebra p-2 print:hidden">
      {MODULES.map((m) => (
        <button
          key={m.id}
          aria-current={module === m.id ? "page" : undefined}
          onClick={() => setModule(m.id)}
          className={`rounded-[4px] px-2.5 py-2 text-left transition-colors ${module === m.id ? "bg-ink text-white" : "text-ink hover:bg-hover"}`}
        >
          <div className="flex items-center gap-2">
            <span className={`grid h-5 w-5 place-items-center rounded-full font-mono text-[11px] ${module === m.id ? "bg-paper text-ink" : "bg-paper text-muted outline outline-1 outline-rule"}`}>{m.n}</span>
            <span className="text-[13px] font-semibold">{m.label}</span>
            {live === m.id && run?.status !== "done" && <span title="The run is working here" className={`ml-auto h-2 w-2 rounded-full ${module === m.id ? "bg-accent" : "bg-link"} animate-pulse`} />}
          </div>
          <div className={`mt-0.5 pl-7 text-[11px] ${module === m.id ? "text-desk-muted" : "text-muted"}`}>{m.stages}</div>
        </button>
      ))}
      {run && run.status !== "stopped" && (
        <div className="mt-auto border-t border-rule p-2 pt-3">
          <div className="text-[11px] font-medium text-muted">Demo control</div>
          <button
            className="mt-1.5 w-full rounded-[4px] border border-dashed border-warn px-2 py-1.5 text-[12px] font-medium text-warn hover:bg-warn-bg"
            title="Corrupts one digit in a few quotes so the checker can catch them. Simulated."
            onClick={async () => {
              try {
                const r = await api.injectErrors(run.run_id, 3);
                setNote(`Corrupted one digit in ${r.item_ids.join(", ")}. Watch the checker catch it.`);
              } catch (e) {
                setNote(e instanceof Error ? e.message : String(e));
              }
            }}
          >
            Inject errors (simulated)
          </button>
          {note && <div className="mt-1 text-[11px] text-muted">{note}</div>}
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
        <main className="min-w-0 flex-1 overflow-y-auto px-8 py-6 print:overflow-visible print:p-0">
          {error && (
            <p role="alert" className="text-bad">
              {error}
            </p>
          )}
          {!runId && !error && <p className="text-muted">Pick a run above, or start a new one.</p>}
          {loading && <Skeleton rows={10} />}
          {run && !loading && (
            <>
              {module === "sources" && <SourcesModule />}
              {module === "report" && <ReportModule />}
              {module === "brief" && <BriefModule />}
            </>
          )}
        </main>
        <div className="w-[360px] flex-none border-l border-desk-line bg-desk print:hidden">{run && <Inspector />}</div>
      </div>
      <div className="print:hidden">{run && <Drawer />}</div>
    </div>
  );
}
