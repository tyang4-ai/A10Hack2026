"use client";

import { useMemo, useState } from "react";
import type { Claim, Report, RunDetail, Source } from "@/lib/api";
import { isFailed, isWithheld } from "@/lib/ui";
import { ClaimActions } from "./Actions";
import { Chart } from "./Charts";
import { Footnote, PageHead, Section } from "./Shell";

const kpiLabel = (t: string) => t.split(/ (was|were|reached|stayed) /)[0].replace(/ for FY\d+$/, "");

export function ReportPanel({ run, report, claims, sources, refresh }: { run: RunDetail; report: Report | null; claims: Claim[]; sources: Source[]; refresh: () => Promise<void> }) {
  const byId = useMemo(() => new Map(claims.map((c) => [c.id, c])), [claims]);
  const [picked, setPicked] = useState<string | null>(null);
  const withheld = claims.filter(isWithheld);

  if (!report) {
    return (
      <div className="animate-[fade-in_200ms_ease-out]">
        <PageHead over="GENERATED REPORT · NOT INVESTMENT ADVICE" title="Report" run={run}>
          <strong>Abstract.</strong> The report is written from verified claims only, after step 10.
        </PageHead>
        <div className="mt-6 grid grid-cols-2 gap-6">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="h-44 animate-pulse bg-zebra" />
          ))}
        </div>
      </div>
    );
  }

  const kpis = (report.sections[0]?.paragraphs.flatMap((p) => p.claim_ids) ?? []).map((id) => byId.get(id)!).filter(Boolean).slice(0, 4);
  const sel = byId.get(picked ?? "") ?? kpis[1] ?? kpis[0] ?? null;

  return (
    <div className="flex animate-[fade-in_200ms_ease-out] flex-wrap items-start gap-7">
      <div className="min-w-0 flex-[999_1_640px]">
        <PageHead over="EQUITY RESEARCH · GENERATED REPORT · NOT INVESTMENT ADVICE" title={report.title} run={run} />

        <div className="mt-4 grid grid-cols-2 border-y border-ink sm:grid-cols-4">
          {kpis.map((k) => (
            <button
              key={k.id}
              onClick={() => setPicked(k.id)}
              className={`border-r border-rule px-3 py-2 text-left transition-colors last:border-r-0 hover:bg-zebra ${sel?.id === k.id ? "bg-[#FFF6DE] shadow-[inset_0_-2px_0_#000]" : ""}`}
            >
              <div className="font-mono text-[10px] tracking-[0.12em] text-muted">{kpiLabel(k.text).toUpperCase()}</div>
              <div className="font-mono text-[26px] font-medium tabular-nums">{k.value}</div>
              <div className="font-mono text-[11px]">
                <span className="text-good">✓ {k.id}</span> <span className="text-link">{k.citations.map((c) => c.source_id).join(", ")}</span>
              </div>
            </button>
          ))}
        </div>

        {report.sections.map((s, i) => (
          <Section key={s.heading} n={`0${i + 1}`} title={s.heading}>
            {s.paragraphs.map((p) => (
              <p key={p.text} className="mb-1.5 text-justify indent-6">
                {p.text.split(/(\[S\d+\])/).map((part, j) =>
                  /^\[S\d+\]$/.test(part) ? (
                    <button key={j} onClick={() => setPicked(p.claim_ids[0])} className="text-link hover:underline">
                      {part}
                    </button>
                  ) : (
                    part
                  ),
                )}
              </p>
            ))}
          </Section>
        ))}

        <div className="mt-6 grid grid-cols-1 gap-x-7 gap-y-6 md:grid-cols-2">
          {report.charts.map((c, i) => (
            <Chart key={c.id} spec={c} n={i + 1} byId={byId} onPick={setPicked} />
          ))}
        </div>

        {withheld.length > 0 && (
          <div className="mt-5 grid grid-cols-1 gap-3 md:grid-cols-2">
            {withheld.map((w) => (
              <div key={w.id} className="animate-[fade-in_250ms_ease-out] border border-dashed border-bad px-3 py-2.5 font-mono text-[11px]">
                <button onClick={() => setPicked(w.id)} className="font-bold tracking-[0.08em] text-bad hover:underline">
                  WITHHELD · {w.id}
                </button>
                <div className="my-1 font-serif text-[15px]">{w.text}</div>
                <div className="text-bad">
                  {["pass", "position_off"].includes(w.code_check.status) ? w.agent_check.reason : w.code_check.detail}
                </div>
                <ClaimActions runId={run.id} claim={w} refresh={refresh} />
              </div>
            ))}
          </div>
        )}
        <p className="mt-3.5 text-[13px] italic">Research analysis, not investment advice. Scenarios describe possible outcomes, not recommendations to buy or sell.</p>
        <Footnote />
      </div>

      {sel && <Provenance claim={sel} sources={sources} />}
    </div>
  );
}

function Provenance({ claim, sources }: { claim: Claim; sources: Source[] }) {
  const ct = claim.citations[0];
  const src = sources.find((s) => s.id === ct?.source_id);
  const failed = isWithheld(claim);
  const nodes: { kind: string; label: string; sub: string; edge?: [boolean | null, string] }[] = [
    {
      kind: "SOURCE",
      label: `${src?.id ?? "?"} ${src?.title ?? ""}`,
      sub: `T${src?.tier} · ${src?.approval === "human" ? `rank ${src.priority ?? "–"} · approved by a person` : "auto-approved"}`,
      edge: [src?.status === "fetched", src?.chars ? `stored · ${src.chars.toLocaleString()} chars` : "not fetched"],
    },
    {
      kind: "QUOTE",
      label: `“${ct?.quote ?? ""}”`,
      sub: ct?.found_at === null ? "not found in stored text" : ct && ct.found_at !== ct.start ? `@${ct.found_at.toLocaleString()} (agent said ${ct.start.toLocaleString()})` : `@${ct?.start.toLocaleString()}`,
      edge: [
        claim.code_check.status === "pass" ? true : claim.code_check.status === "position_off" ? null : false,
        `quote check · ${claim.code_check.status.replace("_", " ")}`,
      ],
    },
    {
      kind: `CLAIM ${claim.id}`,
      label: claim.value ? `${claim.text.split(/ (was|were|reached|stayed) /)[0]} = ${claim.value}` : claim.text,
      sub: claim.kind,
      edge: [claim.agent_check.verdict === "supported", claim.agent_check.validator ? `${claim.agent_check.validator} · ${claim.agent_check.verdict}` : claim.agent_check.verdict],
    },
    { kind: failed ? "WITHHELD" : "ON PAGE", label: claim.value ?? claim.id, sub: failed ? "kept out of the report" : "renderer filled the verified value" },
  ];
  return (
    <aside aria-label="Provenance" className="min-w-0 flex-[1_1_320px] bg-desk font-mono text-xs text-desk-ink lg:sticky lg:top-[100px] lg:max-w-[400px]">
      <div className="flex justify-between border-b border-desk-line px-3.5 py-2.5">
        <span className="font-bold tracking-[0.14em]">PROVENANCE</span>
        <span className="text-desk-muted">{claim.id}</span>
      </div>
      <div className="border-b border-desk-line px-3.5 py-3.5">
        <div className={`text-[30px] font-medium ${failed ? "text-desk-bad" : "text-white"}`}>{claim.value ?? claim.id}</div>
        <div className="text-desk-muted">{claim.text}</div>
      </div>
      <div role="img" aria-label="Provenance chain" className="p-3.5">
        {nodes.map((n, i) => (
          <div key={n.kind}>
            <div className={`border px-2.5 py-2 ${i === nodes.length - 1 ? (failed ? "border-desk-bad bg-[#2A0E0E]" : "border-amber bg-[#1A1500]") : "border-[#3A3A3A]"}`}>
              <div className="text-[10px] tracking-[0.12em] text-desk-muted">{n.kind}</div>
              <div className="text-white">{n.label}</div>
              <div className="text-[11px] text-desk-muted">{n.sub}</div>
            </div>
            {n.edge && (
              <div className="ml-4 flex min-h-[30px] items-stretch gap-2.5">
                <div className="relative w-0 border-l border-[#555]">
                  <span className="absolute -bottom-px -left-1 border-x-4 border-t-[6px] border-x-transparent border-t-[#555]" />
                </div>
                <span className="self-center text-[11px]">
                  <span className={`font-bold ${n.edge[0] === true ? "text-desk-good" : n.edge[0] === null ? "text-amber" : "text-desk-bad"}`}>
                    {n.edge[0] === true ? "✓" : n.edge[0] === null ? "!" : "✕"}
                  </span>{" "}
                  {n.edge[1]}
                </span>
              </div>
            )}
          </div>
        ))}
      </div>
    </aside>
  );
}
