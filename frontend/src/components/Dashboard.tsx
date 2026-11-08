"use client";

import { useEffect, useState } from "react";
import { api, usingMock } from "@/lib/api";
import { useRun } from "@/lib/useRun";
import { isFailed } from "@/lib/ui";
import { Progress, Ticker, TopBar, type Tab } from "@/components/desk/Shell";
import { Tape } from "@/components/desk/Tape";
import { SourcesPanel } from "@/components/desk/SourcesPanel";
import { ClaimsPanel, VerifyPanel } from "@/components/desk/VerifyPanel";
import { ReportPanel } from "@/components/desk/ReportPanel";

export function Dashboard() {
  const [runId, setRunId] = useState<string | null>(null);
  const [company, setCompany] = useState("Siemens Healthineers");
  const [tab, setTab] = useState<Tab>("sources");
  const { run, events, sources, claims, report, error, refresh } = useRun(runId);
  const live = !!run && (run.status === "running" || run.status === "awaiting_approval");
  const stop = () => runId && api.stopRun(runId, "stopped from dashboard").then(refresh);

  // Follow the run: jump to the panel for the current stage once.
  const stage = !run ? "sources" : run.current_step <= 5 ? "sources" : run.current_step <= 9 ? "verify" : "report";
  const [followed, setFollowed] = useState<string>("sources");
  useEffect(() => {
    if (stage !== followed) {
      setFollowed(stage);
      setTab(stage);
    }
  }, [stage, followed]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.shiftKey && e.key === "S" && live && !(e.target instanceof HTMLInputElement)) stop();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const problems = claims.filter(isFailed).length;

  return (
    <div className="flex min-h-screen flex-col">
      <header className="sticky top-0 z-10">
        <TopBar
          run={run}
          tab={tab}
          onTab={setTab}
          onStop={stop}
          counts={{
            sources: { n: sources.filter((s) => s.status !== "removed").length },
            verify: { n: problems, bad: true },
            claims: { n: claims.length },
          }}
        />
        <Ticker />
      </header>

      {!runId ? (
        <main className="mx-auto w-full max-w-[1280px] px-6 py-10">
          <div className="font-mono text-[11px] tracking-[0.12em] text-muted">NEW RUN · STEP 01 / 12</div>
          <h1 className="mt-0.5 text-[26px]">Research a company</h1>
          <p className="mt-2 max-w-[640px] text-[15px]">
            Axelrod proposes sources, waits for you to approve the primary ones, then drafts a report in which every number is checked against
            its source before you see it.
          </p>
          <form
            className="mt-5 flex flex-wrap items-end gap-4 font-mono"
            onSubmit={async (e) => {
              e.preventDefault();
              const r = await api.createRun(company);
              setRunId(r.id);
            }}
          >
            <label className="flex min-w-72 flex-col text-[10px] tracking-[0.1em] text-muted">
              COMPANY
              <input value={company} onChange={(e) => setCompany(e.target.value)} className="border-b border-ink bg-transparent py-1 font-serif text-lg tracking-normal text-ink outline-none focus:border-b-2" />
            </label>
            <button type="submit" className="min-h-10 border border-ink bg-ink px-5 font-bold tracking-[0.08em] text-white">
              START RUN <kbd className="ml-1.5 border border-white px-1 font-normal">⏎</kbd>
            </button>
          </form>
          {usingMock && <p className="mt-4 font-mono text-[11px] text-muted">MOCK BACKEND · set NEXT_PUBLIC_API_URL to use the real one</p>}
        </main>
      ) : run ? (
        <main className="mx-auto w-full max-w-[1280px] px-6 pt-5 pb-10">
          <Progress run={run} />
          <div className="mt-5 flex flex-wrap items-start gap-7">
            <div className="min-w-0 flex-[999_1_640px]">
              {tab === "sources" && <SourcesPanel run={run} sources={sources} events={events} refresh={refresh} />}
              {tab === "verify" && <VerifyPanel run={run} claims={claims} sources={sources} events={events} />}
              {tab === "claims" && <ClaimsPanel run={run} claims={claims} sources={sources} />}
              {tab === "report" && <ReportPanel run={run} report={report} claims={claims} sources={sources} />}
            </div>
            {tab === "sources" && <Tape events={events} live={live} />}
          </div>
        </main>
      ) : (
        <main className="mx-auto w-full max-w-[1280px] px-6 py-10 font-mono text-xs">
          {error ? (
            <p>
              <span className="text-bad">Could not load {runId}: {error}</span>{" "}
              <button onClick={() => setRunId(null)} className="ml-2 border border-ink px-2.5 py-1 hover:bg-ink hover:text-white">
                NEW RUN
              </button>
            </p>
          ) : (
            <span className="text-muted">Loading run…</span>
          )}
        </main>
      )}
    </div>
  );
}
