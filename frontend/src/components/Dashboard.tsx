"use client";

import { useEffect, useRef } from "react";
import { useRunStore } from "@/lib/store";
import { usePolling } from "@/lib/usePolling";
import { LAYOUT_FOR_STAGE, type BuiltIn, useWindows } from "@/lib/windows";
import { Desk, Dock } from "./Desk";
import { Palette } from "./Palette";
import { Banner, TopBar } from "./TopBar";

export function Dashboard({ runId: urlRunId }: { runId: string }) {
  const runId = useRunStore((s) => s.runId);
  const stage = useRunStore((s) => s.run?.stage);
  const error = useRunStore((s) => s.error);
  const hydrated = useWindows((s) => s.hydrated);
  const { loadRuns, openRun } = useRunStore.getState();
  usePolling(runId);

  // Open the run named in the URL, and list runs for the run switcher.
  useEffect(() => {
    loadRuns().catch(() => useRunStore.setState({ error: "Cannot reach the API. Is the mock running on port 8000?" }));
  }, [loadRuns]);
  useEffect(() => {
    if (useRunStore.getState().runId !== urlRunId) void openRun(urlRunId);
  }, [urlRunId, openRun]);

  // Switching runs closes windows pinned to the old run's agents, pages and items.
  const prevRun = useRef<string | null>(null);
  const lastPreset = useRef<BuiltIn | null>(null);
  useEffect(() => {
    if (prevRun.current && prevRun.current !== runId) useWindows.getState().closePinned();
    prevRun.current = runId;
    lastPreset.current = null;
  }, [runId]);

  // Follow the run: when the stage moves to a step with another preset, apply it.
  // If the analyst has moved windows since the last layout, suggest it instead.
  useEffect(() => {
    if (!stage || !hydrated) return;
    const target = LAYOUT_FOR_STAGE[stage];
    if (lastPreset.current === target) return;
    lastPreset.current = target;
    const w = useWindows.getState();
    if (target === w.layout) w.setSuggest(null);
    // A profile the analyst saved is a deliberate choice: suggest, don't override it.
    else if (w.dirty || w.customs.some((c) => c.id === w.layout)) w.setSuggest(target);
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
