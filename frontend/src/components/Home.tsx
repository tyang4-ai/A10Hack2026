"use client";

// The home page: every run, grouped by company. Each company is a project and
// each run is one session of the harness on it. Click a run to open its desk.
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Run } from "@contract/types";
import { api, ApiError } from "@/lib/api";
import { STAGE_LABEL, usd } from "@/lib/format";
import { useRunStore } from "@/lib/store";
import { forgetRunPages } from "./SourcePage";
import { ago, STATUS_DOT, STATUS_WORD, ThemeSwitcher } from "./Switchers";
import { btnDanger, btnSm, Skeleton } from "./ui";

// Fixed widths so every company's table lines up with the others. The last column holds Delete.
const COLUMNS: [string, string][] = [
  ["Run", "w-[24%]"],
  ["Status", "w-[18%]"],
  ["Stage", "w-[12%]"],
  ["Items checked", "w-[14%]"],
  ["Blocked", "w-[8%]"],
  ["Cost", "w-[8%]"],
  ["Updated", "w-[9%]"],
  ["Actions", "w-[7%]"],
];

const LIVE = new Set<Run["status"]>(["running", "awaiting_approval"]);
const FINISHED = new Set<Run["status"]>(["done", "stopped", "failed"]);

// The demo run is never offered for deletion here, and the server refuses it anyway (409).
// Set at build time as a comma-separated list. The default is the DEMO RUN (R2) and its backup (demo-ok-1).
const DEMO_RUNS = new Set(
  (process.env.NEXT_PUBLIC_DEMO_RUN_IDS ?? "run_20261108_rzpoj7,run_20261108_pin2ve")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean),
);

// Only a finished run can go. Replays are shared recordings, so the server refuses those too.
const deletable = (r: Run) => FINISHED.has(r.status) && r.mode !== "replay" && !DEMO_RUNS.has(r.run_id);

// Drop every localStorage and sessionStorage key that names the run. Storage may be blocked.
function dropStoredKeys(runId: string) {
  for (const pick of [() => window.localStorage, () => window.sessionStorage]) {
    try {
      const s = pick();
      const keys: string[] = [];
      for (let i = 0; i < s.length; i++) {
        const k = s.key(i);
        if (k && k.includes(runId)) keys.push(k);
      }
      for (const k of keys) s.removeItem(k);
    } catch {
      // Storage blocked: nothing was cached there.
    }
  }
}

type Outcome = "deleted" | "kept" | "failed";

export function Home() {
  const router = useRouter();
  const pathname = usePathname();
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // Per-row messages: the server's reason for a 409, or a failed request.
  const [rowMsg, setRowMsg] = useState<Record<string, string>>({});
  // Deleted here, so a list fetched just before the delete can't bring a row back.
  const gone = useRef(new Set<string>());

  // Refresh every 2 s while a run is live, so status and stage stay current; every 10 s otherwise.
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      let live = false;
      try {
        const r = await api.listRuns();
        if (!alive) return;
        setRuns(r.runs.filter((x) => !gone.current.has(x.run_id)));
        setErr(null);
        live = r.runs.some((x) => LIVE.has(x.status));
      } catch {
        if (alive) setErr("Can't reach the Axelrod server. Reload to try again.");
      }
      if (alive) timer = setTimeout(tick, live ? 2000 : 10000);
    };
    void tick();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, []);

  // After a delete: drop the row, and everything this tab cached for the run.
  const forget = useCallback(
    (id: string) => {
      gone.current.add(id);
      setRuns((rs) => rs?.filter((x) => x.run_id !== id) ?? rs);
      setRowMsg(({ [id]: _drop, ...rest }) => rest);
      const store = useRunStore.getState();
      if (store.runId === id) void store.openRun(null);
      useRunStore.setState((s) => ({ runs: s.runs.filter((x) => x.run_id !== id) }));
      forgetRunPages(id);
      dropStoredKeys(id);
      if (pathname !== "/" && pathname.includes(encodeURIComponent(id))) router.push("/");
    },
    [pathname, router],
  );

  // One delete. A 404 means it is already gone, which is what we wanted.
  const remove = useCallback(
    async (id: string, quiet: boolean): Promise<Outcome> => {
      try {
        await api.deleteRun(id);
        forget(id);
        return "deleted";
      } catch (x) {
        if (x instanceof ApiError && x.status === 404) {
          forget(id);
          return "deleted";
        }
        const kept = x instanceof ApiError && x.status === 409;
        if (!quiet || !kept) setRowMsg((m) => ({ ...m, [id]: x instanceof Error ? x.message : String(x) }));
        return kept ? "kept" : "failed";
      }
    },
    [forget],
  );

  const projects = useMemo(() => {
    const by = new Map<string, Run[]>();
    for (const r of [...(runs ?? [])].sort((a, b) => b.updated_at.localeCompare(a.updated_at))) by.set(r.company, [...(by.get(r.company) ?? []), r]);
    return [...by.entries()].map(([company, list]) => ({ company, ticker: list.find((r) => r.ticker)?.ticker ?? null, runs: list }));
  }, [runs]);

  const live = runs?.filter((r) => LIVE.has(r.status)).length ?? 0;
  const spend = runs?.reduce((s, r) => s + r.usage.cost_usd, 0) ?? 0;

  return (
    <div className="relative min-h-screen bg-paper text-ink">
      <div className="absolute top-3 right-4">
        <ThemeSwitcher />
      </div>

      {/* Search-first, like a search engine: the wordmark, then one box to start researching a company. */}
      <header className="mx-auto flex max-w-2xl flex-col items-center px-4 pt-[14vh] pb-10 text-center">
        <h1 className="font-logo text-[64px] leading-none font-semibold tracking-[0.01em] text-ink">Axelrod</h1>
        <p className="mt-3 text-[14px] text-muted">Validated Financial Market reports</p>
        <StartSearch />
      </header>

      <main className="mx-auto max-w-6xl px-4 pb-12 sm:px-6">
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2">
          <h2 className="text-[22px] font-semibold text-ink">Projects</h2>
          {runs && <BulkDelete runs={runs} remove={remove} />}
        </div>

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
            <div className="flex gap-1.5" title="Metered at the gateway">
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
        {runs && runs.length === 0 && <p className="mt-6 text-muted">No projects yet.</p>}

        {projects.map((p) => (
          <section key={p.company} aria-labelledby={`p-${p.company}`} className="mt-8">
            <div className="mb-2 flex flex-wrap items-baseline gap-x-3">
              <h3 id={`p-${p.company}`} className="text-[15px] font-semibold text-ink">
                {p.company}
              </h3>
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
                        {h === "Actions" ? <span className="sr-only">{h}</span> : h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {p.runs.map((r) => (
                    <RunRow
                      key={r.run_id}
                      r={r}
                      msg={rowMsg[r.run_id] ?? null}
                      onDelete={() => remove(r.run_id, false)}
                      onDismiss={() => setRowMsg(({ [r.run_id]: _drop, ...rest }) => rest)}
                    />
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

// Delete every finished run except the demo run, after one question. The server keeps
// protected runs (409), so those are skipped quietly and counted as kept.
function BulkDelete({ runs, remove }: { runs: Run[]; remove: (id: string, quiet: boolean) => Promise<Outcome> }) {
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const targets = runs.filter(deletable);

  const go = async () => {
    setBusy(true);
    setResult(null);
    const out = { deleted: 0, kept: 0, failed: 0 };
    for (const r of targets) out[await remove(r.run_id, true)]++;
    const left = runs.length - out.deleted - out.failed;
    setResult(`${out.deleted} deleted, ${left} kept (protected or running)${out.failed ? `, ${out.failed} failed` : ""}.`);
    setBusy(false);
    setAsking(false);
  };

  if (asking)
    return (
      <div role="group" aria-label="Confirm deleting other runs" className="flex flex-wrap items-center gap-2 text-[13px]" onKeyDown={(e) => e.key === "Escape" && !busy && setAsking(false)}>
        <span>
          Delete {targets.length} finished run{targets.length === 1 ? "" : "s"}? Their events and files go. The demo run, replays and live runs stay.
        </span>
        <button type="button" className={btnDanger} disabled={busy || !targets.length} onClick={() => void go()}>
          {busy ? "Deleting" : "Yes"}
        </button>
        <button type="button" autoFocus className={btnSm} disabled={busy} onClick={() => setAsking(false)}>
          Cancel
        </button>
      </div>
    );

  return (
    <div className="flex flex-wrap items-center gap-3 text-[13px]">
      {result && (
        <span role="status" className="text-muted">
          {result}
        </span>
      )}
      <button
        type="button"
        className={btnSm}
        disabled={!targets.length}
        title={targets.length ? "Delete every finished run except the demo run" : "No finished runs to delete"}
        onClick={() => {
          setResult(null);
          setAsking(true);
        }}
      >
        Delete other runs
      </button>
    </div>
  );
}

// A rerun names the run it came from and the step it reran from.
function Lineage({ r }: { r: Run }) {
  const x = r;
  if (!x.parent_run_id) return null;
  return (
    <div className="text-[12px] text-muted" title={x.instructions ?? undefined}>
      Rerun of{" "}
      <Link href={`/runs/${encodeURIComponent(x.parent_run_id)}`} className="font-mono text-link hover:underline">
        {x.parent_run_id.split("_").pop()}
      </Link>
      {x.rerun_from ? ` from ${STAGE_LABEL[x.rerun_from]}` : ""}
    </div>
  );
}

// The search box starts a run on the company typed in, then opens its desk.
function StartSearch() {
  const router = useRouter();
  const [company, setCompany] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <form
      className="mt-8 w-full"
      onSubmit={async (e) => {
        e.preventDefault();
        if (!company.trim()) return;
        setBusy(true);
        setErr(null);
        try {
          const run = await api.createRun({ company: company.trim(), created_via: "ui" });
          router.push(`/runs/${encodeURIComponent(run.run_id)}`);
        } catch (x) {
          setErr(x instanceof Error ? x.message : String(x));
          setBusy(false);
        }
      }}
    >
      <label htmlFor="start-company" className="sr-only">
        Company to research
      </label>
      <div className="flex h-12 items-center gap-2 rounded-full border border-control bg-paper pr-1.5 pl-5 focus-within:border-ink focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-focus">
        <svg aria-hidden width="16" height="16" viewBox="0 0 16 16" className="flex-none text-muted">
          <circle cx="7" cy="7" r="5" fill="none" stroke="currentColor" strokeWidth="1.5" />
          <path d="M11 11l3.5 3.5" stroke="currentColor" strokeWidth="1.5" />
        </svg>
        <input
          id="start-company"
          autoFocus
          value={company}
          onChange={(e) => setCompany(e.target.value)}
          placeholder="Which company should we look into?"
          className="h-full min-w-0 flex-1 bg-transparent text-[15px] text-ink outline-none placeholder:text-muted"
        />
        <button disabled={busy || !company.trim()} className="h-9 flex-none rounded-full bg-ink px-4 text-[13px] font-semibold text-paper disabled:opacity-40">
          {busy ? "Starting" : "Start run"}
        </button>
      </div>
      {err && (
        <p role="alert" className="mt-2 text-bad">
          {err}
        </p>
      )}
    </form>
  );
}

function RunRow({ r, msg, onDelete, onDismiss }: { r: Run; msg: string | null; onDelete: () => Promise<Outcome>; onDismiss: () => void }) {
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const c = r.counts;
  const checked = c.items_pass + c.items_fixed;
  const href = `/runs/${encodeURIComponent(r.run_id)}`;
  const canDelete = deletable(r);
  const open = asking || !!msg;
  return (
    <Fragment>
      <tr className={`hover:bg-zebra ${open ? "" : "border-b border-rule"}`}>
        <td className="px-2 py-2">
          <Link href={href} className="flex items-center gap-2 font-mono text-link hover:underline" title={`Open ${r.run_id}`}>
            {r.run_id}
          </Link>
          <Lineage r={r} />
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
        <td className="px-2 py-2 text-right whitespace-nowrap">
          {canDelete && !asking && (
            <button
              type="button"
              className="text-[12px] text-muted hover:text-bad hover:underline"
              aria-label={`Delete ${r.run_id}`}
              onClick={() => setAsking(true)}
            >
              Delete
            </button>
          )}
          {DEMO_RUNS.has(r.run_id) && <span className="text-[12px] text-muted" title="The demo run is kept">Demo</span>}
        </td>
      </tr>
      {open && (
        <tr className="border-b border-rule">
          <td colSpan={COLUMNS.length} className="px-2 pb-2">
            {asking ? (
              <div
                role="group"
                aria-label={`Confirm deleting ${r.run_id}`}
                className="flex flex-wrap items-center gap-2 text-[13px]"
                onKeyDown={(e) => e.key === "Escape" && !busy && setAsking(false)}
              >
                <span>Delete this run? Its events and files go.</span>
                <button
                  type="button"
                  className={btnDanger}
                  disabled={busy}
                  onClick={async () => {
                    setBusy(true);
                    const out = await onDelete();
                    // A deleted row unmounts. Otherwise the server's reason shows below.
                    if (out !== "deleted") {
                      setBusy(false);
                      setAsking(false);
                    }
                  }}
                >
                  {busy ? "Deleting" : "Yes"}
                </button>
                <button type="button" autoFocus className={btnSm} disabled={busy} onClick={() => setAsking(false)}>
                  Cancel
                </button>
              </div>
            ) : (
              <p role="alert" className="flex flex-wrap items-center gap-2 text-[12px] text-bad">
                {msg}
                <button type="button" className="text-muted hover:text-ink hover:underline" onClick={onDismiss}>
                  Dismiss
                </button>
              </p>
            )}
          </td>
        </tr>
      )}
    </Fragment>
  );
}
