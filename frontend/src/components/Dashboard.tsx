"use client";

import { useEffect, useRef } from "react";
import { moduleFor, type Module } from "@/lib/format";
import { useRunStore } from "@/lib/store";
import { usePolling } from "@/lib/usePolling";
import { LAYOUT_FOR_MODULE, useWindows } from "@/lib/windows";
import { Desk, Dock } from "./Desk";
import { Palette } from "./Palette";
import { Banner, TopBar } from "./TopBar";

export function Dashboard() {
  const runId = useRunStore((s) => s.runId);
  const stage = useRunStore((s) => s.run?.stage);
  const error = useRunStore((s) => s.error);
  const hydrated = useWindows((s) => s.hydrated);
  const { loadRuns, openRun } = useRunStore.getState();
  usePolling(runId);

  // First load: list runs and open the newest.
  useEffect(() => {
    loadRuns().then(
      () => {
        const runs = useRunStore.getState().runs;
        if (!useRunStore.getState().runId && runs[0]) void openRun(runs[0].run_id);
      },
      () => useRunStore.setState({ error: "Cannot reach the API. Is the mock running on port 8000?" }),
    );
  }, [loadRuns, openRun]);

  // Switching runs closes windows pinned to the old run's agents, pages and items.
  const prevRun = useRef<string | null>(null);
  const lastModule = useRef<Module | null>(null);
  useEffect(() => {
    if (prevRun.current && prevRun.current !== runId) useWindows.getState().closePinned();
    prevRun.current = runId;
    lastModule.current = null;
  }, [runId]);

  // Follow the run: when the stage moves to another module, apply that module's
  // layout. If the analyst has moved windows since the last layout, suggest it instead.
  useEffect(() => {
    if (!stage || !hydrated) return;
    const m = moduleFor(stage);
    if (lastModule.current === m) return;
    lastModule.current = m;
    const target = LAYOUT_FOR_MODULE[m];
    const w = useWindows.getState();
    if (target === w.layout) w.setSuggest(null);
    else if (w.dirty) w.setSuggest(target);
    else w.applyLayout(target);
  }, [stage, runId, hydrated]);

  return (
    <div className="flex h-screen flex-col overflow-hidden print:block print:h-auto print:overflow-visible">
      <div className="print:hidden">
        <TopBar />
        <Banner />
        {error && (
          <p role="alert" className="bg-desk px-4 pb-2 text-desk-bad">
            {error}
          </p>
        )}
      </div>
      <Desk />
      <Dock />
      <Palette />
    </div>
  );
}
