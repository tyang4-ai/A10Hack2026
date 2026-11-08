"use client";

import { useMemo, useState } from "react";
import type { Claim, RunDetail, RunEvent, Source } from "@/lib/api";
import { codeLabel, isFailed } from "@/lib/ui";
import { Footnote, PageHead, Section, StatRow } from "./Shell";

const failedCode = (c: Claim) => ["not_found", "value_mismatch", "tier_violation", "source_removed"].includes(c.code_check.status);

function why(c: Claim) {
  if (failedCode(c)) return codeLabel[c.code_check.status].toUpperCase();
  return c.agent_check.verdict.toUpperCase();
}

export function VerifyPanel({ run, claims, sources, events }: { run: RunDetail; claims: Claim[]; sources: Source[]; events: RunEvent[] }) {
  const problems = claims.filter(isFailed);
  const [picked, setPicked] = useState<string | null>(null);
  const sel = claims.find((c) => c.id === picked) ?? problems[0] ?? null;

  const funnel = useMemo(() => {
    const n = claims.length;
    const found = claims.filter((c) => !["not_found", "source_removed", "pending"].includes(c.code_check.status)).length;
    const tierOk = claims.filter((c) => ["pass", "position_off"].includes(c.code_check.status)).length;
    const supported = claims.filter((c) => c.agent_check.verdict === "supported").length;
    const rows: [string, number, number][] = [
      ["EXTRACTED", n, 0],
      ["QUOTE FOUND", found, n - found],
      ["TIER OK", tierOk, found - tierOk],
      ["SUPPORTED", supported, claims.filter((c) => c.agent_check.verdict === "misleading" || c.agent_check.verdict === "unsupported").length],
    ];
    return { n: Math.max(1, n), rows };
  }, [claims]);

  const lanes = useMemo(() => {
    const by = new Map<string, { start: number; end: number | null; checks: number }>();
    for (const e of events) {
      if (!e.actor.id.startsWith("validator:")) continue;
      const t = new Date(e.ts).getTime();
      const l = by.get(e.actor.id) ?? { start: t, end: null, checks: 0 };
      if (e.type === "check") {
        l.end = t;
        l.checks++;
      }
      by.set(e.actor.id, l);
    }
    const all = [...by.entries()];
    const t0 = Math.min(...all.map(([, l]) => l.start));
    const now = events.length ? new Date(events[events.length - 1].ts).getTime() : t0;
    const span = Math.max(6000, ...all.map(([, l]) => (l.end ?? now) - t0));
    return all.map(([id, l]) => ({
      id,
      left: ((l.start - t0) / span) * 100,
      width: Math.max(1, (((l.end ?? now) - l.start) / span) * 100),
      done: l.end !== null,
      secs: l.end ? ((l.end - l.start) / 1000).toFixed(1) + "s" : "READING",
    }));
  }, [events]);

  const matrix = useMemo(() => {
    const srcIds = [...new Set(claims.flatMap((c) => c.citations.map((ct) => ct.source_id)))].sort((a, b) => +a.slice(1) - +b.slice(1));
    return srcIds.map((sid) => ({
      sid,
      tier: sources.find((s) => s.id === sid)?.tier,
      load: claims.filter((c) => c.citations.some((ct) => ct.source_id === sid)).length,
      cells: claims.map((c) => {
        if (!c.citations.some((ct) => ct.source_id === sid)) return "·";
        if (isFailed(c)) return "✕";
        return c.agent_check.verdict === "supported" ? "■" : "○";
      }),
    }));
  }, [claims, sources]);
  const heaviest = matrix.reduce((a, b) => (b.load > (a?.load ?? 0) ? b : a), matrix[0]);

  if (claims.length === 0) {
    return (
      <div className="animate-[fade-in_200ms_ease-out]">
        <PageHead over="STEPS 07–08 / 12 · QUOTE CHECK & VALIDATORS" title="Verification" run={run}>
          <strong>Abstract.</strong> Nothing to verify yet. Claims appear here once the initial report is drafted (step 6).
        </PageHead>
        <div className="mt-6 grid gap-2">{[0, 1, 2, 3].map((i) => <div key={i} className="h-6 animate-pulse bg-zebra" />)}</div>
      </div>
    );
  }

  return (
    <div className="flex animate-[fade-in_200ms_ease-out] flex-wrap items-start gap-7">
      <div className="min-w-0 flex-[999_1_600px]">
        <PageHead over="STEPS 07–08 / 12 · QUOTE CHECK & VALIDATORS" title="Verification" run={run}>
          <strong>Abstract.</strong> {claims.length} claims extracted. Code checked every quote against the stored sources;{" "}
          <span className="text-bad">{claims.filter(failedCode).length} failed</span>. {lanes.length} validators read the rest in parallel;{" "}
          <span className="text-bad">{claims.filter((c) => !failedCode(c) && isFailed(c)).length} judged misleading or unsupported</span>.
        </PageHead>

        <div className="mt-5">
          <StatRow
            stats={[
              { label: "CLAIMS", value: claims.length },
              { label: "QUOTE CHECKED", value: claims.filter((c) => c.code_check.status !== "pending").length },
              { label: "VALIDATED", value: claims.filter((c) => c.agent_check.verdict !== "pending").length },
              { label: "PROBLEMS", value: problems.length, bad: problems.length > 0 },
            ]}
          />
        </div>

        <div className="mt-5 flex flex-wrap gap-7">
          <figure className="flex-[1_1_300px]">
            <div className="mb-2 font-mono text-[10px] tracking-[0.12em] text-muted">CLAIM FUNNEL</div>
            {funnel.rows.map(([label, n, loss]) => (
              <div key={label} className="mb-1.5 grid grid-cols-[110px_minmax(0,1fr)_30px] items-center gap-2 font-mono text-[11px]">
                <span>{label}</span>
                <div className="relative h-4 bg-[#EDEDED]">
                  <div className="absolute inset-y-0 left-0 bg-ink transition-[width] duration-500" style={{ width: `${(n / funnel.n) * 100}%` }} />
                  <div className="absolute inset-y-0 bg-bad transition-all duration-500" style={{ left: `${(n / funnel.n) * 100}%`, width: `${(loss / funnel.n) * 100}%` }} />
                </div>
                <span className="text-right font-bold tabular-nums">{n}</span>
              </div>
            ))}
            <figcaption className="mt-1.5 text-[13px]">
              <strong>Figure 1.</strong> Claims surviving each check. <span className="text-bad">Red</span>: dropped at that stage.
            </figcaption>
          </figure>

          <figure className="flex-[1_1_340px]">
            <div className="mb-2 font-mono text-[10px] tracking-[0.12em] text-muted">VALIDATOR TIMELINE</div>
            {lanes.length === 0 && <div className="font-mono text-[11px] text-muted">Validators start at step 8.</div>}
            {lanes.map((l) => (
              <div key={l.id} className="mb-1 grid grid-cols-[92px_minmax(0,1fr)_60px] items-center gap-2 font-mono text-[11px]">
                <span className="truncate">{l.id.replace("validator:", "val:")}</span>
                <div className="relative h-3 border-l border-ink">
                  <div
                    className={`absolute inset-y-0 transition-all duration-500 ${l.done ? "bg-ink" : "border border-r-0 border-ink"}`}
                    style={{ left: `${l.left}%`, width: `${l.width}%` }}
                  />
                </div>
                <span className={l.done ? "tabular-nums" : "italic"}>{l.secs}</span>
              </div>
            ))}
            <figcaption className="mt-1.5 text-[13px]">
              <strong>Figure 2.</strong> Validators run in parallel. Open bars are still reading.
            </figcaption>
          </figure>
        </div>

        <Section n="01" title="Source coverage">
          <div className="overflow-x-auto">
            <table aria-label="Which sources support which claims" className="border-collapse font-mono text-[11px]">
              <thead>
                <tr>
                  <th className="py-0.5 pr-2.5 text-left font-medium text-muted">SRC \ CLAIM</th>
                  {claims.map((c) => (
                    <th key={c.id} className="w-[30px] text-center font-medium">
                      <button onClick={() => setPicked(c.id)} className={`hover:underline ${sel?.id === c.id ? "underline" : ""}`}>
                        {c.id}
                      </button>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {matrix.map((r) => (
                  <tr key={r.sid} className="border-t border-[#E2E2E2]">
                    <td className="py-0.5 pr-2.5 whitespace-nowrap">
                      <strong>{r.sid}</strong> <span className="text-muted">T{r.tier}</span>
                    </td>
                    {r.cells.map((m, i) => (
                      <td key={i} className={`text-center text-[13px] transition-colors ${m === "✕" ? "bg-[#FBEAEA] text-bad" : m === "·" ? "text-[#BDBDBD]" : ""}`}>
                        {m}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-1.5 text-[13px]">
            <strong>Figure 3.</strong> Rows are sources (S1 = source 1) with their trust tier; columns are claims. ■ supported, <span className="text-bad">✕ failed</span>, ○ reading.
            {heaviest && heaviest.load >= 5 && ` ${heaviest.sid} carries ${heaviest.load} claims: worth a second primary source.`}
          </p>
        </Section>

        <Section n="02" title="Problems">
          <table className="w-full border-y border-ink font-mono text-xs">
            <tbody>
              {problems.length === 0 && (
                <tr>
                  <td className="py-2 text-good">No problems so far.</td>
                </tr>
              )}
              {problems.map((p) => (
                <tr key={p.id} onClick={() => setPicked(p.id)} className={`cursor-pointer border-t border-[#E2E2E2] first:border-t-0 hover:bg-zebra ${sel?.id === p.id ? "bg-zebra" : ""}`}>
                  <td className="py-1.5 pr-2 font-bold">{p.id}</td>
                  <td className="px-2 font-serif text-[15px]">{p.text}</td>
                  <td className="px-2 whitespace-nowrap">{p.citations.map((c) => c.source_id).join(", ")}</td>
                  <td className="pl-2 font-bold whitespace-nowrap text-bad">{why(p)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
        <Footnote />
      </div>

      {sel && <Inspector claim={sel} sources={sources} />}
    </div>
  );
}

export function Inspector({ claim, sources }: { claim: Claim; sources: Source[] }) {
  const bad = isFailed(claim);
  const ct = claim.citations[0];
  const src = sources.find((s) => s.id === ct?.source_id);
  const status = claim.code_check.status;
  return (
    <aside aria-label="Inspector" className="min-w-0 flex-[1_1_320px] bg-desk font-mono text-xs text-desk-ink lg:sticky lg:top-[100px] lg:max-w-[400px]">
      <div className="flex items-center justify-between border-b border-desk-line px-3.5 py-2.5">
        <span className="font-bold tracking-[0.14em]">INSPECTOR · {claim.id}</span>
        <span className={`px-1.5 font-bold ${bad ? "bg-desk-bad text-desk" : status === "pending" ? "text-desk-muted" : "text-desk-good"}`}>{why(claim)}</span>
      </div>
      <div className="border-b border-desk-line px-3.5 py-3">
        <div className={`font-serif text-[17px] ${bad ? "text-desk-bad" : "text-white"}`}>{claim.text}</div>
        {ct && (
          <>
            <div className="mt-2 text-desk-muted">
              CITED · {ct.source_id} {src?.title} · T{src?.tier} · @{ct.start.toLocaleString()}
            </div>
            <div className="mt-1.5 border-l-2 border-[#555] px-2.5 py-2">“{ct.quote}”</div>
          </>
        )}
      </div>
      <div className="border-b border-desk-line px-3.5 py-3">
        <div className="mb-2 text-[10px] tracking-[0.12em] text-desk-muted">QUOTE CHECK</div>
        <div className="flex gap-2.5">
          <span className={`font-bold ${["pass"].includes(status) ? "text-desk-good" : status === "position_off" ? "text-amber" : status === "pending" ? "text-desk-muted" : "text-desk-bad"}`}>
            {status === "pass" ? "✓" : status === "position_off" ? "!" : status === "pending" ? "…" : "✕"}
          </span>
          <span>{claim.code_check.detail || "Waiting for step 7."}</span>
        </div>
        {ct && ct.found_at !== null && ct.found_at !== ct.start && (
          <div className="mt-1 text-desk-muted">
            claimed @{ct.start.toLocaleString()} · found @{ct.found_at.toLocaleString()}
          </div>
        )}
      </div>
      <div className="px-3.5 py-3">
        <div className="mb-2 text-[10px] tracking-[0.12em] text-desk-muted">VALIDATOR</div>
        <div className="flex gap-2.5">
          <span className={`font-bold ${claim.agent_check.verdict === "supported" ? "text-desk-good" : claim.agent_check.verdict === "pending" ? "text-desk-muted" : "text-desk-bad"}`}>
            {claim.agent_check.verdict === "supported" ? "✓" : claim.agent_check.verdict === "pending" ? "…" : "✕"}
          </span>
          <span>
            {claim.agent_check.validator ? `${claim.agent_check.validator} · ` : ""}
            {claim.agent_check.reason || (failedCode(claim) ? "Skipped: failed the quote check." : "Waiting for step 8.")}
          </span>
        </div>
      </div>
    </aside>
  );
}

export function ClaimsPanel({ run, claims, sources }: { run: RunDetail; claims: Claim[]; sources: Source[] }) {
  const [picked, setPicked] = useState<string | null>(null);
  const sel = claims.find((c) => c.id === picked) ?? claims[0] ?? null;
  const kinds: Claim["kind"][] = ["data", "conclusion", "extrapolation"];
  return (
    <div className="flex animate-[fade-in_200ms_ease-out] flex-wrap items-start gap-7">
      <div className="min-w-0 flex-[999_1_600px]">
        <PageHead over="CLAIM LEDGER · DATA → CONCLUSIONS → EXTRAPOLATIONS" title="Claims" run={run}>
          <strong>Abstract.</strong> Every statement in the initial report, with its citation, quote check and validator verdict.
        </PageHead>
        {claims.length === 0 && <p className="mt-6 font-mono text-xs text-muted">Claims appear after the initial report is drafted (step 6).</p>}
        {kinds.map((k, i) => {
          const rows = claims.filter((c) => c.kind === k);
          if (!rows.length) return null;
          return (
            <Section key={k} n={`0${i + 1}`} title={k === "data" ? "Data" : k === "conclusion" ? "Conclusions" : "Extrapolations"}>
              <div className="overflow-x-auto">
                <table className="w-full border-y border-ink font-mono text-xs">
                  <thead>
                    <tr className="border-b border-ink text-left text-[10px] tracking-[0.1em] text-muted">
                      <th className="py-1.5 pr-2 font-medium">ID</th>
                      <th className="px-2 font-medium">CLAIM</th>
                      <th className="px-2 text-right font-medium">VALUE</th>
                      <th className="px-2 font-medium">SOURCE</th>
                      <th className="px-2 font-medium">QUOTE</th>
                      <th className="pl-2 font-medium">VALIDATOR</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((c) => (
                      <tr key={c.id} onClick={() => setPicked(c.id)} className={`cursor-pointer hover:bg-zebra ${sel?.id === c.id ? "bg-zebra" : ""}`}>
                        <td className="py-1 pr-2 font-bold">{c.id}</td>
                        <td className="px-2 font-serif text-[15px]">{c.text}</td>
                        <td className="px-2 text-right tabular-nums">{c.value ?? "—"}</td>
                        <td className="px-2 whitespace-nowrap">{c.citations.map((x) => x.source_id).join(", ")}</td>
                        <td className={`px-2 whitespace-nowrap ${failedCode(c) ? "text-bad" : c.code_check.status === "pass" ? "text-good" : c.code_check.status === "position_off" ? "italic" : "text-muted"}`}>
                          {codeLabel[c.code_check.status].toUpperCase()}
                        </td>
                        <td className={`pl-2 whitespace-nowrap ${c.agent_check.verdict === "supported" ? "text-good" : c.agent_check.verdict === "pending" ? "text-muted" : "text-bad"}`}>
                          {c.agent_check.verdict.toUpperCase()}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Section>
          );
        })}
        <Footnote />
      </div>
      {sel && <Inspector claim={sel} sources={sources} />}
    </div>
  );
}
