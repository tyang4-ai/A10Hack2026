import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Claim, type Report, type RunDetail, type RunEvent, type Source } from "./api";

export interface RunData {
  run: RunDetail | null;
  events: RunEvent[];
  sources: Source[];
  claims: Claim[];
  report: Report | null;
  error: string | null;
  refresh: () => Promise<void>;
}

const POLL_MS = 1000;

// Polls the backend for one run. Events are fetched incrementally by seq.
export function useRun(runId: string | null): RunData {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [claims, setClaims] = useState<Claim[]>([]);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const lastSeq = useRef(0);

  const busy = useRef(false);

  const refresh = useCallback(async () => {
    if (!runId || busy.current) return;
    busy.current = true;
    try {
      const [r, ev, src, cl, rep] = await Promise.all([
        api.getRun(runId),
        api.listEvents(runId, lastSeq.current),
        api.listSources(runId),
        api.listClaims(runId),
        api.getReport(runId),
      ]);
      setRun(r);
      if (ev.length) {
        lastSeq.current = ev[ev.length - 1].seq;
        setEvents((prev) => [...prev, ...ev]);
      }
      setSources(src);
      setClaims(cl);
      setReport(rep);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      busy.current = false;
    }
  }, [runId]);

  useEffect(() => {
    lastSeq.current = 0;
    setEvents([]);
    setRun(null);
    if (!runId) return;
    void refresh();
    const t = setInterval(refresh, POLL_MS);
    return () => clearInterval(t);
  }, [runId, refresh]);

  return { run, events, sources, claims, report, error, refresh };
}
