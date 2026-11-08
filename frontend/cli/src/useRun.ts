// Keeps one run live in the terminal. Polls GET /events after the last seq every
// second (every 5 s once the run is finished) for gateway traffic, and refetches the
// snapshots (run, plan, report, final report, agents, policy) whenever events arrive.
import { useEffect, useRef, useState } from "react";
import type { Agent, AgentReport, GatewayRequestEvent, HumanReport, Item, Plan, Policy, Run } from "../../../contract/types.ts";
import { isEvent } from "../../../contract/types.ts";
import { api } from "./api.ts";

export interface RunData {
  run: Run | null;
  plan: Plan | null;
  items: Record<string, Item>;
  report: AgentReport | null;
  summary: HumanReport | null;
  agents: Agent[];
  policy: Policy | null;
  traffic: GatewayRequestEvent[];
  error: string | null;
}

const EMPTY: RunData = { run: null, plan: null, items: {}, report: null, summary: null, agents: [], policy: null, traffic: [], error: null };
const FINISHED = new Set(["done", "stopped", "failed"]);
const TRAFFIC_CAP = 2000;

export function useRun(runId: string): RunData & { refresh: () => void } {
  const [data, setData] = useState<RunData>(EMPTY);
  const lastSeq = useRef(0);
  const kick = useRef<() => void>(() => {});

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    lastSeq.current = 0;
    setData(EMPTY);

    const snapshots = async () => {
      const [run, plan, report, summary, agents, policy] = await Promise.all([
        api.getRun(runId),
        api.plan(runId),
        api.report(runId),
        api.summary(runId),
        api.agents(runId).catch(() => null),
        api.policy(runId).catch(() => null),
      ]);
      if (!alive) return;
      setData((d) => ({
        ...d,
        run,
        plan,
        report,
        items: Object.fromEntries(report.items.map((i) => [i.id, i])),
        summary,
        agents: agents?.agents ?? d.agents,
        policy: policy ?? d.policy,
        error: null,
      }));
    };

    const tick = async () => {
      let delay = 1000;
      try {
        let fresh = lastSeq.current === 0;
        // Drain the event log a page at a time; traffic rows come from gateway.request events.
        for (;;) {
          const page = await api.events(runId, lastSeq.current);
          if (!alive) return;
          if (page.events.length) {
            fresh = true;
            lastSeq.current = page.events[page.events.length - 1].seq;
            const rows = page.events.filter((e): e is GatewayRequestEvent => isEvent(e, "gateway.request"));
            if (rows.length) setData((d) => ({ ...d, traffic: [...d.traffic, ...rows].slice(-TRAFFIC_CAP) }));
          }
          if (page.events.length < 500) {
            if (FINISHED.has(page.run_status)) delay = 5000;
            break;
          }
        }
        if (fresh) await snapshots();
      } catch (e) {
        if (alive) setData((d) => ({ ...d, error: e instanceof Error ? e.message : String(e) }));
        delay = 3000;
      }
      if (alive) timer = setTimeout(tick, delay);
    };

    kick.current = () => {
      clearTimeout(timer);
      void snapshots().then(() => alive && (timer = setTimeout(tick, 0)));
    };
    void tick();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [runId]);

  return { ...data, refresh: () => kick.current() };
}
