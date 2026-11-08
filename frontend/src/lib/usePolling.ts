"use client";

import { useEffect } from "react";
import { api } from "./api";
import { useRunStore } from "./store";

const LIMIT = 500;

// Polls GET /events?after=last_seq: every 1 s while the run is live, every 5 s
// once it is done, stopped or failed, and immediately again when a page is full.
export function usePolling(runId: string | null) {
  useEffect(() => {
    if (!runId) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;

    const tick = async () => {
      let delay = 1000;
      try {
        const st = useRunStore.getState();
        if (st.runId !== runId || st.loading) {
          timer = setTimeout(tick, 200);
          return;
        }
        const page = await api.events(runId, st.lastSeq, LIMIT);
        if (!alive || useRunStore.getState().runId !== runId) return;
        st.apply(page.events);
        if (page.events.length) void st.refreshRun();
        const finished = page.run_status === "done" || page.run_status === "stopped" || page.run_status === "failed";
        delay = page.events.length >= LIMIT ? 0 : finished ? 5000 : 1000;
      } catch {
        delay = 3000;
      }
      if (alive) timer = setTimeout(tick, delay);
    };
    timer = setTimeout(tick, 0);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [runId]);
}
