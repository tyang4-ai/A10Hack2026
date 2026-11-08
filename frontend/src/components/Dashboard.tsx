"use client";

import { useState } from "react";
import { api, usingMock } from "@/lib/api";
import { useRun } from "@/lib/useRun";
import { Pipeline } from "@/components/Pipeline";
import { Traffic } from "@/components/Traffic";

// Placeholder shell: starts a run against the API (or the mock) and shows the
// pipeline and gateway traffic. The real panels follow the design canvas.
export function Dashboard() {
  const [runId, setRunId] = useState<string | null>(null);
  const [company, setCompany] = useState("Siemens Healthineers");
  const { run, events } = useRun(runId);

  if (!runId) {
    return (
      <main className="mx-auto w-full max-w-5xl px-8 py-10">
        <h1 className="text-2xl">Axelrod</h1>
        <form
          className="mt-4 flex flex-wrap items-end gap-4"
          onSubmit={async (e) => {
            e.preventDefault();
            const r = await api.createRun(company);
            setRunId(r.id);
          }}
        >
          <label className="flex flex-col text-sm italic">
            Company
            <input
              className="border-b border-ink bg-transparent py-1 text-base not-italic outline-none"
              value={company}
              onChange={(e) => setCompany(e.target.value)}
            />
          </label>
          <button type="submit" className="border border-ink px-4 py-1.5 [font-variant:small-caps] tracking-wide">
            Start run
          </button>
        </form>
        {usingMock && <p className="mt-3 font-mono text-xs text-muted">Using the built-in mock backend.</p>}
      </main>
    );
  }

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-wrap items-start gap-6 px-6 py-6">
      {run && <Pipeline steps={run.steps} />}
      <section className="min-w-0 flex-[999_1_480px]">
        <h1 className="text-2xl">
          {run?.company} <span className="font-mono text-sm text-muted">{run?.status}</span>
        </h1>
        <div className="mt-3 flex gap-2">
          {run?.status === "awaiting_approval" && (
            <button className="border border-ink px-3 py-1 [font-variant:small-caps]" onClick={() => api.approveSources(runId)}>
              Approve sources
            </button>
          )}
          <button className="border border-ink px-3 py-1 [font-variant:small-caps]" onClick={() => api.stopRun(runId, "stopped from dashboard")}>
            Stop run
          </button>
        </div>
      </section>
      <Traffic events={events} />
    </main>
  );
}
