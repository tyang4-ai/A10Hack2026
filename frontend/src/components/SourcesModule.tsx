"use client";

import { useEffect, useMemo, useState } from "react";
import type { Criterion, Source, SourceDecision, SourceKind, Tier } from "@contract/types";
import rubric from "@contract/boffin_rubric.json";
import { api, ApiError } from "@/lib/api";
import { human } from "@/lib/format";
import { useRunStore } from "@/lib/store";
import { btn, btnPrimary, Label, SourceChip } from "./ui";

const SCREEN = (rubric as { depths: { screen: Criterion[] } }).depths.screen;
const TIER_LABEL: Record<Tier, string> = { priority: "Priority", tier_2: "Tier 2", tier_3: "Tier 3" };
const KINDS: SourceKind[] = ["annual_report", "sec_filing", "xbrl_facts", "company_ir", "company_site", "product_page", "news", "gov", "market_report", "forum", "code_repo", "data_api", "other"];

type Decision = "approved" | "rejected" | null;

export function SourcesModule() {
  const run = useRunStore((s) => s.run)!;
  const needs = useRunStore((s) => s.needs);
  const sourcesMap = useRunStore((s) => s.sources);
  const planReady = useRunStore((s) => s.planReady);
  const selected = useRunStore((s) => s.selected);
  const select = useRunStore((s) => s.select);
  const refreshPlan = useRunStore((s) => s.refreshPlan);
  const awaiting = run.status === "awaiting_approval";

  const sources = useMemo(
    () => Object.values(sourcesMap).sort((a, b) => a.tier.localeCompare(b.tier) || a.rank - b.rank),
    [sourcesMap],
  );

  // Draft approval: decisions per source and the priority order, editable until committed.
  const [decisions, setDecisions] = useState<Record<string, Decision>>({});
  const [order, setOrder] = useState<string[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState<string | null>(null);

  useEffect(() => {
    const prio = sources.filter((s) => s.tier === "priority").map((s) => s.source_id);
    setOrder((o) => [...o.filter((id) => prio.includes(id)), ...prio.filter((id) => !o.includes(id))]);
  }, [sources]);

  const decisionOf = (s: Source): Decision => {
    if (!awaiting) return s.approval === "rejected" ? "rejected" : s.approval === "pending" ? null : "approved";
    if (decisions[s.source_id] !== undefined) return decisions[s.source_id];
    if (s.tier !== "priority" || s.proposed_by === "user") return "approved";
    return null;
  };

  const priority = order.map((id) => sourcesMap[id]).filter(Boolean);
  const lower = sources.filter((s) => s.tier !== "priority");
  const undecided = priority.filter((s) => decisionOf(s) === null);

  // Coverage: which in-scope criteria would have no approved source left.
  const coverage = useMemo(() => {
    const live = sources.filter((s) => decisionOf(s) !== "rejected" && s.status !== "revoked");
    const covered = new Set(live.flatMap((s) => s.criteria));
    return SCREEN.filter((c) => !covered.has(c)).map((c) => ({
      criterion: c,
      cause: sources.filter((s) => s.criteria.includes(c) && (decisionOf(s) === "rejected" || s.status === "revoked")).map((s) => s.source_id),
    }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sources, decisions, awaiting]);

  const move = (id: string, to: number) =>
    setOrder((o) => {
      const next = o.filter((x) => x !== id);
      next.splice(Math.max(0, Math.min(next.length, to)), 0, id);
      return next;
    });

  const commit = async () => {
    setBusy(true);
    setErr(null);
    const req: SourceDecision[] = [
      ...priority.map((s, i) => ({ source_id: s.source_id, approval: decisionOf(s) as "approved" | "rejected", tier: "priority" as Tier, rank: i + 1 })),
      ...lower.filter((s) => decisionOf(s) === "rejected").map((s) => ({ source_id: s.source_id, approval: "rejected" as const })),
    ];
    try {
      await api.approve(run.run_id, { decisions: req });
      await Promise.all([refreshPlan(), useRunStore.getState().refreshRun()]);
    } catch (e) {
      if (e instanceof ApiError && e.details?.undecided) setErr(`Decide these first: ${(e.details.undecided as string[]).join(", ")}`);
      else setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="animate-[fade-in_200ms_ease-out]">
      <Label>STAGES PLAN · APPROVE · COLLECT</Label>
      <h1 className="mt-0.5 text-[26px]">Sources</h1>
      <p className="mt-1 max-w-[760px] text-justify text-[15px]">
        <strong>Abstract.</strong>{" "}
        {awaiting
          ? `Nothing is fetched until you approve. Decide every priority source, drag to set the order, then approve. Lower tiers are approved automatically unless you reject them.`
          : run.status === "stopped"
            ? "The run was stopped. The kill switch denies every request, so nothing more is fetched."
            : run.stage === "plan"
              ? "The planner is mapping BOFFIN criteria to data needs and proposing sources."
            : "Approval is committed. Approved sources form the gateway allowlist; everything else is denied by default. Revoke a source to cut it off mid-run."}
      </p>

      <details className="mt-4" open={run.stage === "plan"}>
        <summary className="cursor-pointer font-mono text-[11px] tracking-[0.1em]">DATA NEEDS · {needs.length}</summary>
        <ul className="mt-2 grid gap-1.5 md:grid-cols-2">
          {needs.map((n) => (
            <li key={n.need_id} className="animate-[fade-in_250ms_ease-out] border-l-2 border-ink pl-2.5">
              <div className="font-mono text-[10px] text-muted">
                {n.need_id} · {human(n.dimension).toUpperCase()} · {n.criteria.map(human).join(", ")}
              </div>
              <div className="font-bold">{n.label}</div>
              <div className="text-sm">{n.question}</div>
            </li>
          ))}
          {needs.length === 0 && <li className="font-mono text-xs text-muted">Waiting for the planner…</li>}
        </ul>
      </details>

      {coverage.length > 0 && (
        <div role="status" className="mt-4 border border-bad px-3 py-2 text-sm">
          <strong className="font-mono text-[11px] tracking-[0.08em] text-bad">COVERAGE WARNING</strong>
          {coverage.map((c) => (
            <div key={c.criterion}>
              {c.cause.length ? `Rejecting ${c.cause.join(", ")} leaves ` : ""}
              <strong>{human(c.criterion)}</strong> with no source. It will become a hole.
            </div>
          ))}
        </div>
      )}

      <section className="mt-5">
        <div className="mb-1.5 flex items-baseline gap-3">
          <h2 className="text-lg font-bold">
            <span className="mr-2.5 font-mono text-[13px]">01</span>Priority
          </h2>
          {awaiting && (
            <>
              <span className="font-mono text-[11px] text-muted">{undecided.length ? `${undecided.length} undecided` : "all decided"}</span>
              <button
                className={btn}
                onClick={() => setDecisions((d) => ({ ...d, ...Object.fromEntries(undecided.map((s) => [s.source_id, "approved" as const])) }))}
                disabled={!undecided.length}
              >
                APPROVE REST
              </button>
            </>
          )}
        </div>
        <ol className="border-y border-ink">
          {priority.map((s, i) => (
            <li
              key={s.source_id}
              draggable={awaiting}
              onDragStart={() => setDrag(s.source_id)}
              onDragOver={(e) => e.preventDefault()}
              onDrop={() => drag && move(drag, i)}
              onDragEnd={() => setDrag(null)}
              className={`border-b border-rule last:border-b-0 ${drag === s.source_id ? "opacity-40" : ""}`}
            >
              <SourceRow
                s={s}
                rank={i + 1}
                decision={decisionOf(s)}
                awaiting={awaiting}
                selected={selected?.type === "source" && selected.id === s.source_id}
                onSelect={() => select({ type: "source", id: s.source_id })}
                onDecide={(d) => setDecisions((x) => ({ ...x, [s.source_id]: d }))}
                onMove={(d) => move(s.source_id, i + d)}
              />
            </li>
          ))}
          {priority.length === 0 && <li className="py-3 font-mono text-xs text-muted">{planReady ? "No priority sources." : "Waiting for proposals…"}</li>}
        </ol>
      </section>

      {(["tier_2", "tier_3"] as Tier[]).map((t, k) => {
        const list = lower.filter((s) => s.tier === t);
        if (!list.length) return null;
        return (
          <section key={t} className="mt-5">
            <h2 className="mb-1.5 text-lg font-bold">
              <span className="mr-2.5 font-mono text-[13px]">0{k + 2}</span>
              {TIER_LABEL[t]} <span className="font-mono text-[11px] font-normal text-muted">· auto-approved</span>
            </h2>
            <ul className="border-y border-ink">
              {list.map((s) => (
                <li key={s.source_id} className="border-b border-rule last:border-b-0">
                  <SourceRow
                    s={s}
                    decision={decisionOf(s)}
                    awaiting={awaiting}
                    selected={selected?.type === "source" && selected.id === s.source_id}
                    onSelect={() => select({ type: "source", id: s.source_id })}
                    onDecide={(d) => setDecisions((x) => ({ ...x, [s.source_id]: d }))}
                  />
                </li>
              ))}
            </ul>
          </section>
        );
      })}

      {awaiting && <AddSource runId={run.run_id} />}

      {awaiting && (
        <div className="sticky bottom-0 mt-5 flex flex-wrap items-center gap-3 border-t border-ink bg-white py-3">
          <button className={`${btnPrimary} min-h-10 px-5`} disabled={busy || undecided.length > 0} onClick={commit}>
            APPROVE &amp; RUN
          </button>
          <span className="font-mono text-[11px] text-muted">
            {undecided.length ? `Decide ${undecided.length} more priority source${undecided.length > 1 ? "s" : ""} first.` : "Sends one approval with every decision and your order."}
          </span>
          {err && <span className="font-mono text-[11px] text-bad">{err}</span>}
        </div>
      )}
    </div>
  );
}

function SourceRow({
  s,
  rank,
  decision,
  awaiting,
  selected,
  onSelect,
  onDecide,
  onMove,
}: {
  s: Source;
  rank?: number;
  decision: Decision;
  awaiting: boolean;
  selected: boolean;
  onSelect: () => void;
  onDecide: (d: Decision) => void;
  onMove?: (d: number) => void;
}) {
  const runId = useRunStore((x) => x.runId)!;
  const refreshPlan = useRunStore((x) => x.refreshPlan);
  const [revoking, setRevoking] = useState(false);
  const rejected = decision === "rejected";
  const canRevoke = !awaiting && (s.approval === "approved" || s.approval === "auto_approved") && s.status !== "revoked";
  return (
    <div className={`flex animate-[fade-in_250ms_ease-out] items-start gap-3 px-1 py-2 ${selected ? "bg-zebra" : ""} ${rejected ? "opacity-50" : ""}`}>
      {rank !== undefined && (
        <span className={`w-5 pt-0.5 text-right font-mono text-sm font-bold ${awaiting ? "cursor-grab" : ""}`} title={awaiting ? "Drag to reorder" : undefined}>
          {rank}
        </span>
      )}
      <button onClick={onSelect} className="min-w-0 flex-1 text-left">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="font-mono text-[11px] text-muted">{s.source_id}</span>
          <span className={`text-[15px] ${rejected ? "line-through" : ""}`}>{s.title}</span>
          {s.proposed_by === "user" && <span className="font-mono text-[10px] text-[#1A6FB0]">ADDED BY YOU</span>}
        </div>
        <div className="font-mono text-[11px] text-muted">
          {s.domain} · {human(s.kind)} · reliability {Math.round(s.scores.reliability * 100)} · {s.criteria.map(human).join(", ")}
        </div>
      </button>
      <div className="flex flex-none items-center gap-1.5 pt-0.5">
        {!awaiting && <SourceChip status={s.status} />}
        {awaiting && onMove && (
          <>
            <button aria-label={`Move ${s.source_id} up`} className={`${btn} px-1.5`} onClick={() => onMove(-1)}>
              ↑
            </button>
            <button aria-label={`Move ${s.source_id} down`} className={`${btn} px-1.5`} onClick={() => onMove(1)}>
              ↓
            </button>
          </>
        )}
        {awaiting && (
          <div role="group" aria-label={`Decision for ${s.source_id}`} className="flex">
            <button
              aria-pressed={decision === "approved"}
              onClick={() => onDecide("approved")}
              className={`border border-ink px-2 py-1 font-mono text-[11px] ${decision === "approved" ? "bg-ink text-white" : "hover:bg-zebra"}`}
            >
              APPROVE
            </button>
            <button
              aria-pressed={decision === "rejected"}
              onClick={() => onDecide("rejected")}
              className={`border border-l-0 border-ink px-2 py-1 font-mono text-[11px] ${decision === "rejected" ? "bg-bad text-white" : "hover:bg-zebra"}`}
            >
              REJECT
            </button>
          </div>
        )}
        {canRevoke && (
          <button
            disabled={revoking}
            className="border border-bad px-2 py-1 font-mono text-[11px] text-bad transition-colors hover:bg-bad hover:text-white disabled:opacity-40"
            onClick={async () => {
              setRevoking(true);
              try {
                await api.revoke(runId, s.source_id, "Revoked by the analyst");
                await refreshPlan();
              } finally {
                setRevoking(false);
              }
            }}
          >
            REVOKE
          </button>
        )}
      </div>
    </div>
  );
}

function AddSource({ runId }: { runId: string }) {
  const [url, setUrl] = useState("");
  const [kind, setKind] = useState<SourceKind>("other");
  const [key, setKey] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [added, setAdded] = useState<Source | null>(null);
  const refreshPlan = useRunStore((s) => s.refreshPlan);
  return (
    <form
      className="mt-5 border-t border-rule pt-3"
      onSubmit={async (e) => {
        e.preventDefault();
        setErr(null);
        try {
          const s = await api.addSource(runId, { url, kind, api_key: key || null });
          setAdded(s);
          setUrl("");
          setKey("");
          await refreshPlan();
        } catch (x) {
          setErr(x instanceof Error ? x.message : String(x));
        }
      }}
    >
      <Label>ADD A SOURCE</Label>
      <div className="mt-1.5 flex flex-wrap items-end gap-3 font-mono text-xs">
        <label className="flex flex-[2_1_260px] flex-col text-[10px] tracking-[0.1em] text-muted">
          URL
          <input required type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://" className="border-b border-ink bg-transparent py-1 text-[13px] text-ink outline-none" />
        </label>
        <label className="flex flex-col text-[10px] tracking-[0.1em] text-muted">
          KIND
          <select value={kind} onChange={(e) => setKind(e.target.value as SourceKind)} className="border-b border-ink bg-transparent py-1 text-[13px] text-ink">
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {human(k)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-[1_1_180px] flex-col text-[10px] tracking-[0.1em] text-muted">
          API KEY (OPTIONAL, NEVER SHOWN AGAIN)
          <input type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} className="border-b border-ink bg-transparent py-1 text-[13px] text-ink outline-none" />
        </label>
        <button className={btn}>+ ADD</button>
      </div>
      {err && <div className="mt-1 font-mono text-[11px] text-bad">{err}</div>}
      {added && (
        <div className="mt-1 font-mono text-[11px] text-good">
          Added {added.source_id} · {added.domain} · {TIER_LABEL[added.tier]}
          {added.access.auth === "api_key" ? " · key stored server-side" : ""}
        </div>
      )}
    </form>
  );
}
