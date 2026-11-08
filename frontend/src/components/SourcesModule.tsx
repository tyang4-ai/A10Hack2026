"use client";

import { useEffect, useMemo, useState } from "react";
import type { Criterion, Source, SourceDecision, SourceKind, Tier } from "@contract/types";
import rubric from "@contract/boffin_rubric.json";
import { api, ApiError } from "@/lib/api";
import { human } from "@/lib/format";
import { useRunStore } from "@/lib/store";
import { btn, btnDanger, btnPrimary, btnSm, Chip, SourceChip } from "./ui";

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

  const sources = useMemo(() => Object.values(sourcesMap).sort((a, b) => a.tier.localeCompare(b.tier) || a.rank - b.rank), [sourcesMap]);

  // Draft approval: a decision per source and the priority order, editable until committed.
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

  // Coverage: in-scope criteria that would have no approved source left.
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
      if (e instanceof ApiError && e.details?.undecided) setErr(`These priority sources still need a decision: ${(e.details.undecided as string[]).join(", ")}.`);
      else setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const intro = awaiting
    ? "Nothing is fetched until you approve. Decide every priority source and drag them into the order you trust most. Tier 2 and 3 are approved automatically unless you reject them."
    : run.status === "stopped"
      ? "The run was stopped. The kill switch denies every request, so nothing more is fetched."
      : run.stage === "plan"
        ? "The planner is mapping BOFFIN criteria to data needs and proposing sources."
        : "Approved sources form the gateway allowlist and everything else is denied. Revoke a source to cut it off mid-run.";

  return (
    <div className="max-w-[920px]">
      <h1 className="text-[22px] font-semibold">Sources</h1>
      <p className="mt-1 max-w-[72ch] text-[14px] text-muted">{intro}</p>

      <details className="mt-5 border-y border-rule py-2" open={run.stage === "plan"}>
        <summary className="cursor-pointer text-[13px] font-semibold">
          Data needs <span className="font-mono font-normal text-muted">{needs.length}</span>
        </summary>
        <ul className="mt-2 grid gap-3 pb-1 md:grid-cols-2">
          {needs.map((n) => (
            <li key={n.need_id}>
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className="font-mono text-[11px] text-muted">{n.need_id}</span>
                <span className="font-semibold">{n.label}</span>
              </div>
              <div className="mt-0.5 text-[13px] text-muted">{n.question}</div>
              <div className="mt-1 flex flex-wrap gap-1">
                <Chip>{human(n.dimension)}</Chip>
                {n.criteria.map((c) => (
                  <Chip key={c}>{human(c)}</Chip>
                ))}
              </div>
            </li>
          ))}
          {needs.length === 0 && <li className="text-muted">Waiting for the planner.</li>}
        </ul>
      </details>

      {coverage.length > 0 && (
        <div role="status" className="mt-4 rounded-[4px] border border-bad/40 bg-[#FEF2F2] px-3 py-2 text-[13px]">
          <div className="font-semibold text-bad">Coverage gap</div>
          {coverage.map((c) => (
            <div key={c.criterion}>
              {c.cause.length ? `Rejecting ${c.cause.join(", ")} leaves ` : "No source covers "}
              <strong>{human(c.criterion)}</strong>
              {c.cause.length ? " with no source. It will become a hole." : ". It will become a hole."}
            </div>
          ))}
        </div>
      )}

      <section className="mt-6">
        <div className="mb-2 flex flex-wrap items-center gap-3">
          <h2 className="text-[15px] font-semibold">Priority</h2>
          {awaiting && (
            <>
              <span className="text-muted">{undecided.length ? `${undecided.length} still need a decision` : "All decided"}</span>
              <button
                className={btnSm}
                onClick={() => setDecisions((d) => ({ ...d, ...Object.fromEntries(undecided.map((s) => [s.source_id, "approved" as const])) }))}
                disabled={!undecided.length}
              >
                Approve the rest
              </button>
            </>
          )}
        </div>
        <ol className="border-t border-ink">
          {priority.map((s, i) => (
            <li
              key={s.source_id}
              draggable={awaiting}
              onDragStart={() => setDrag(s.source_id)}
              onDragOver={(e) => e.preventDefault()}
              onDrop={() => drag && move(drag, i)}
              onDragEnd={() => setDrag(null)}
              className={`border-b border-rule ${drag === s.source_id ? "opacity-40" : ""}`}
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
          {priority.length === 0 && <li className="border-b border-rule py-3 text-muted">{planReady ? "No priority sources." : "Waiting for proposals."}</li>}
        </ol>
      </section>

      {(["tier_2", "tier_3"] as Tier[]).map((t) => {
        const list = lower.filter((s) => s.tier === t);
        if (!list.length) return null;
        return (
          <section key={t} className="mt-6">
            <h2 className="mb-2 text-[15px] font-semibold">
              {TIER_LABEL[t]} <span className="font-normal text-muted">approved automatically</span>
            </h2>
            <ul className="border-t border-ink">
              {list.map((s) => (
                <li key={s.source_id} className="border-b border-rule">
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
        <div className="sticky bottom-0 mt-6 flex flex-wrap items-center gap-3 border-t border-rule bg-white py-3">
          <button className={btnPrimary} disabled={busy || undecided.length > 0} onClick={commit}>
            Approve and run
          </button>
          <span className="text-muted">
            {undecided.length ? `Decide ${undecided.length} more priority source${undecided.length > 1 ? "s" : ""} first.` : "Sends every decision and your order in one approval."}
          </span>
          {err && (
            <span role="alert" className="text-bad">
              {err}
            </span>
          )}
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
  const [revoking, setRevoking] = useState<"idle" | "confirm" | "busy">("idle");
  const rejected = decision === "rejected";
  const running = useRunStore((x) => x.run?.status === "running");
  const canRevoke = running && (s.approval === "approved" || s.approval === "auto_approved") && s.status !== "revoked";
  useEffect(() => {
    if (revoking !== "confirm") return;
    const t = setTimeout(() => setRevoking("idle"), 4000);
    return () => clearTimeout(t);
  }, [revoking]);
  return (
    <div className={`flex items-center gap-3 px-1 py-2.5 ${selected ? "bg-zebra" : "hover:bg-zebra"} ${rejected ? "opacity-55" : ""}`}>
      {rank !== undefined && (
        <span className={`w-6 text-right font-mono text-[13px] font-semibold tabular-nums ${awaiting ? "cursor-grab" : ""}`} title={awaiting ? "Drag to reorder" : `Fetched in order ${rank}`}>
          {rank}
        </span>
      )}
      <button onClick={onSelect} className="min-w-0 flex-1 text-left">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className={`text-[14px] font-medium ${rejected ? "line-through" : ""}`}>{s.title}</span>
          {s.proposed_by === "user" && <span className="text-[11px] font-medium text-link">Added by you</span>}
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-muted">
          <span className="font-mono">{s.source_id}</span>
          <span className="font-mono">{s.domain}</span>
          <span>{human(s.kind)}</span>
          <span title="Feeds the certainty of every item from this source">
            Reliability <span className="font-mono tabular-nums text-ink">{Math.round(s.scores.reliability * 100)}</span>
          </span>
          {s.criteria.map((c) => (
            <Chip key={c}>{human(c)}</Chip>
          ))}
        </div>
      </button>
      <div className="flex flex-none items-center gap-1.5">
        {!awaiting && <SourceChip status={s.status} />}
        {awaiting && onMove && (
          <>
            <button aria-label={`Move ${s.source_id} up`} className={`${btnSm} w-7 px-0`} onClick={() => onMove(-1)}>
              ↑
            </button>
            <button aria-label={`Move ${s.source_id} down`} className={`${btnSm} w-7 px-0`} onClick={() => onMove(1)}>
              ↓
            </button>
          </>
        )}
        {awaiting && (
          <div role="group" aria-label={`Decision for ${s.source_id}`} className="flex overflow-hidden rounded-[4px] border border-[#CBD5E1] text-[12px] font-medium">
            <button aria-pressed={decision === "approved"} onClick={() => onDecide("approved")} className={`h-7 px-2.5 ${decision === "approved" ? "bg-good text-white" : "hover:bg-zebra"}`}>
              Approve
            </button>
            <button
              aria-pressed={decision === "rejected"}
              onClick={() => onDecide("rejected")}
              className={`h-7 border-l border-[#CBD5E1] px-2.5 ${decision === "rejected" ? "bg-bad text-white" : "hover:bg-zebra"}`}
            >
              Reject
            </button>
          </div>
        )}
        {canRevoke && (
          <button
            disabled={revoking === "busy"}
            className={revoking === "confirm" ? `${btnDanger} bg-bad text-white` : btnDanger}
            onClick={async () => {
              if (revoking === "idle") return setRevoking("confirm");
              setRevoking("busy");
              try {
                await api.revoke(runId, s.source_id, "Revoked by the analyst");
                await refreshPlan();
              } finally {
                setRevoking("idle");
              }
            }}
          >
            {revoking === "confirm" ? "Confirm revoke" : "Revoke"}
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
  const field = "h-8 rounded-[4px] border border-[#CBD5E1] bg-white px-2 text-[13px] text-ink outline-none focus:border-ink";
  return (
    <form
      className="mt-6"
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
      <h2 className="text-[15px] font-semibold">Add a source</h2>
      <div className="mt-2 flex flex-wrap items-end gap-3">
        <label className="flex flex-[2_1_260px] flex-col gap-1 text-[12px] text-muted">
          URL
          <input required type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://" className={field} />
        </label>
        <label className="flex flex-col gap-1 text-[12px] text-muted">
          Kind
          <select value={kind} onChange={(e) => setKind(e.target.value as SourceKind)} className={field}>
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {human(k)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-[1_1_180px] flex-col gap-1 text-[12px] text-muted">
          API key (optional, kept on the server)
          <input type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} className={field} />
        </label>
        <button className={btn}>Add source</button>
      </div>
      {err && (
        <div role="alert" className="mt-1.5 text-bad">
          {err}
        </div>
      )}
      {added && (
        <div className="mt-1.5 text-good">
          Added {added.source_id} ({added.domain}) to {TIER_LABEL[added.tier]}
          {added.access.auth === "api_key" ? ". The key is stored on the server and never shown again." : "."}
        </div>
      )}
    </form>
  );
}
