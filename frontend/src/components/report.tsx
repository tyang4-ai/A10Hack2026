"use client";

import type { Chart, Item, ReportCertainty, Rollup } from "@contract/types";
import { BAND_BG, CERTAINTY_HELP, formatAxis, formatValue, human, roundHalfUp, STATUS_TEXT } from "@/lib/format";
import { useRunStore } from "@/lib/store";
import { CertaintyPill, KindBadge, Label, StatusText } from "./ui";

export function itemText(i: Item): string {
  return i.kind === "data" ? i.statement : i.kind === "chart" ? i.title : i.text;
}

// Report certainty and coverage, always side by side and never merged.
export function Gauge({ c, label }: { c: ReportCertainty | null; label: string }) {
  if (!c) return null;
  const segs: [keyof typeof BAND_BG, number][] = [
    ["high", c.high],
    ["medium", c.medium],
    ["low", c.low],
    ["none", c.none],
  ];
  const total = segs.reduce((n, [, v]) => n + v, 0) || 1;
  return (
    <div className="flex flex-wrap items-stretch gap-6">
      <div title={`${CERTAINTY_HELP}\n${c.method}`}>
        <Label>{label} CERTAINTY</Label>
        <div className="font-mono text-[28px] font-medium tabular-nums">{c.mean ?? "—"}</div>
        <div className="mt-1 flex h-2 w-48 overflow-hidden">
          {segs.map(([b, v]) => (
            <div key={b} className={`${BAND_BG[b]} border-r border-white last:border-r-0`} style={{ width: `${(v / total) * 100}%` }} />
          ))}
        </div>
        <div className="mt-1 font-mono text-[10px] text-muted">{c.high_pct}% in the high band</div>
      </div>
      <div className="border-l border-rule pl-6">
        <Label>COVERAGE</Label>
        <div className="font-mono text-[28px] font-medium tabular-nums">
          {c.coverage_scored} <span className="text-base text-muted">of {c.coverage_in_scope}</span>
        </div>
        <div className="mt-1 font-mono text-[10px] text-muted">screen criteria scored</div>
      </div>
    </div>
  );
}

export function Scorecard({ rollup }: { rollup: Rollup | null }) {
  const items = useRunStore((s) => s.items);
  const select = useRunStore((s) => s.select);
  if (!rollup) return <div className="font-mono text-xs text-muted">The scorecard appears when the report is final.</div>;
  const scores = Object.values(items).filter((i) => i.kind === "extrapolation");
  const fmt = (n: number | null) => (n === null ? "N/A" : `${n > 0 ? "+" : ""}${roundHalfUp(n, 2).toFixed(2)}`);
  return (
    <div>
      <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
        <div>
          <Label>WEIGHTED AVERAGE</Label>
          <div className="font-mono text-[28px] font-medium">{fmt(rollup.weighted_average)}</div>
        </div>
        <div className="font-mono text-xs">
          {rollup.normalized_pct !== null && <span>{rollup.normalized_pct}% normalized </span>}
          {rollup.percent_of_max_pct !== null && <span>· {rollup.percent_of_max_pct}% of max </span>}
          <span className="text-muted">(both inference)</span>
        </div>
        <div className="font-mono text-xs">
          Coverage <strong>{rollup.scored} of {rollup.in_scope}</strong> scored
          {rollup.na_criteria.length > 0 && <span className="text-muted"> · N/A: {rollup.na_criteria.map(human).join(", ")}</span>}
        </div>
      </div>
      <table className="mt-2 w-full border-y border-ink font-mono text-xs">
        <thead>
          <tr className="border-b border-ink text-left text-[10px] tracking-[0.1em] text-muted">
            <th className="py-1 font-medium">CATEGORY</th>
            <th className="font-medium">WEIGHT</th>
            <th className="font-medium">SCORE</th>
            <th className="font-medium">SCORED</th>
            <th className="font-medium">CRITERIA</th>
          </tr>
        </thead>
        <tbody>
          {rollup.categories.map((c) => (
            <tr key={c.category} className="border-t border-rule align-top">
              <td className="py-1">{human(c.category)}</td>
              <td>{c.weight}</td>
              <td className="font-bold">{fmt(c.score)}</td>
              <td>
                {c.scored}/{c.in_scope}
              </td>
              <td>
                {scores
                  .filter((e) => e.category === c.category)
                  .map((e) => (
                    <button key={e.id} onClick={() => select({ type: "item", id: e.id })} className="mr-2 hover:underline">
                      {e.kind === "extrapolation" && `${human(e.criterion)} ${e.score === null ? "N/A" : (e.score > 0 ? "+" : "") + e.score}`}{" "}
                      <CertaintyPill c={e.certainty} />
                    </button>
                  ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-1 text-[13px] text-muted">{rollup.note}</p>
    </div>
  );
}

// Charts are specs built by code; points are already computed. Never recompute them.
export function ChartView({ chart, compact }: { chart: Chart; compact?: boolean }) {
  const select = useRunStore((s) => s.select);
  const pts = chart.series.flatMap((s) => s.points.map((p) => ({ ...p, series: s.name })));
  if (chart.chart_type === "table" || pts.length === 0)
    return (
      <table className="w-full font-mono text-xs">
        <tbody>
          {pts.map((p) => (
            <tr key={p.series + p.x} className="border-t border-rule">
              <td>{p.x}</td>
              <td className="text-right">{formatAxis(p.y, chart.unit, chart.scale)}</td>
              <td className="pl-2 text-right text-muted">{p.ref}</td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  const xs = [...new Set(pts.map((p) => p.x))];
  const max = Math.max(...pts.map((p) => p.y), 0) * 1.12 || 1;
  const W = 420;
  const H = compact ? 150 : 180;
  const left = 46;
  const bottom = H - 22;
  const y = (v: number) => bottom - (v / max) * (bottom - 12);
  const bw = Math.min(46, ((W - left - 10) / xs.length) * 0.6);
  const x = (i: number) => left + ((i + 0.5) * (W - left - 10)) / xs.length;
  const ticks = [0, max / 2, max].map((t) => Math.round(t));
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="block w-full font-mono text-[10px]" role="img" aria-label={`${chart.title}: ${pts.map((p) => `${p.x} ${p.y}`).join(", ")}`}>
      {ticks.map((t) => (
        <g key={t}>
          <line x1={left} x2={W - 8} y1={y(t)} y2={y(t)} stroke={t === 0 ? "#000" : "#E2E2E2"} />
          <text x={left - 5} y={y(t) + 3} textAnchor="end" className="fill-muted">
            {formatAxis(t, chart.unit, chart.scale)}
          </text>
        </g>
      ))}
      {chart.chart_type === "line" && (
        <polyline points={pts.map((p) => `${x(xs.indexOf(p.x))},${y(p.y)}`).join(" ")} fill="none" stroke="#000" strokeWidth={2} />
      )}
      {pts.map((p) => {
        const i = xs.indexOf(p.x);
        return (
          <g key={p.series + p.x} onClick={() => select({ type: "item", id: p.ref })} className="cursor-pointer">
            <title>{`${p.x}: ${formatAxis(p.y, chart.unit, chart.scale)} (${p.ref})`}</title>
            {chart.chart_type === "bar" ? (
              <rect x={x(i) - bw / 2} y={y(p.y)} width={bw} height={bottom - y(p.y)} className="fill-ink hover:fill-[#1A6FB0]" />
            ) : (
              <circle cx={x(i)} cy={y(p.y)} r={4} className="fill-white stroke-ink hover:fill-[#1A6FB0]" strokeWidth={1.5} />
            )}
            <text x={x(i)} y={y(p.y) - 5} textAnchor="middle">
              {roundHalfUp(p.y, 1).toLocaleString("en-US")}
            </text>
            <text x={x(i)} y={H - 6} textAnchor="middle" className="fill-muted">
              {p.x}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

export function ReportModule() {
  const report = useRunStore((s) => s.report);
  const items = useRunStore((s) => s.items);
  const selected = useRunStore((s) => s.selected);
  const select = useRunStore((s) => s.select);
  if (!report) return null;
  const list = Object.values(items);
  const status = (k: string) => list.filter((i) => i.verification.status === k).length;
  // Sections from the snapshot; items that arrived after it go into their category.
  const placed = new Set(report.sections.flatMap((s) => s.item_ids));
  const sections = report.sections.map((s) => ({
    ...s,
    item_ids: [...s.item_ids, ...list.filter((i) => !placed.has(i.id) && i.category === s.category).map((i) => i.id)],
  }));
  for (const i of list)
    if (!placed.has(i.id) && !sections.some((s) => s.category === i.category)) sections.push({ category: i.category, title: human(i.category), item_ids: [i.id] });

  return (
    <div className="animate-[fade-in_200ms_ease-out]">
      <Label>STAGES EXTRACT · DRAFT · VERIFY · REPAIR · AGENT-FACING</Label>
      <h1 className="mt-0.5 text-[26px]">Agent report</h1>
      <div className="mt-1 font-mono text-xs">
        <span className="uppercase">{report.status.replace("_", " ")}</span> · {list.length} items ·{" "}
        <span className={STATUS_TEXT.pass}>{status("pass")} pass</span> · <span className={STATUS_TEXT.fixed}>{status("fixed")} fixed</span> ·{" "}
        <span className={STATUS_TEXT.fail}>{status("fail")} fail</span> · <span className="text-muted">{status("pending")} pending</span>
      </div>
      <div className="mt-4">
        <Gauge c={report.certainty} label="REPORT" />
      </div>

      <section className="mt-6">
        <h2 className="mb-2 text-lg font-bold">
          <span className="mr-2.5 font-mono text-[13px]">01</span>Scorecard
        </h2>
        <Scorecard rollup={report.rollup} />
      </section>

      {sections.map((sec, k) => (
        <section key={sec.category} className="mt-6">
          <h2 className="mb-1.5 text-lg font-bold">
            <span className="mr-2.5 font-mono text-[13px]">{String(k + 2).padStart(2, "0")}</span>
            {sec.title}
          </h2>
          <ul className="border-y border-ink">
            {sec.item_ids.map((id) => {
              const it = items[id];
              if (!it)
                return (
                  <li key={id} className="border-b border-rule px-1 py-1 font-mono text-[11px] text-muted italic last:border-b-0">
                    {id} (missing)
                  </li>
                );
              const on = selected?.type === "item" && selected.id === id;
              if (it.kind === "chart")
                return (
                  <li key={id} className={`border-b border-rule px-1 py-2 last:border-b-0 ${on ? "bg-zebra" : ""}`}>
                    <button onClick={() => select({ type: "item", id })} className="flex w-full items-center gap-2 text-left">
                      <KindBadge kind="chart" />
                      <span className="flex-1">{it.title}</span>
                      <StatusText status={it.verification.status} />
                      <CertaintyPill c={it.certainty} />
                    </button>
                    <div className="mt-1 max-w-[560px]">
                      <ChartView chart={it} />
                    </div>
                  </li>
                );
              return (
                <li key={id} className={`animate-[fade-in_250ms_ease-out] border-b border-rule last:border-b-0 ${on ? "bg-zebra" : "hover:bg-[#FAFAFA]"}`}>
                  <button onClick={() => select({ type: "item", id })} className="flex w-full items-start gap-2 px-1 py-1.5 text-left">
                    <KindBadge kind={it.kind} />
                    <span className="w-12 flex-none pt-0.5 font-mono text-[11px] text-muted">{id}</span>
                    <span className={`min-w-0 flex-1 text-[15px] ${it.verification.status === "fail" ? "text-bad line-through decoration-1" : ""}`}>{itemText(it)}</span>
                    {it.kind === "data" && <span className="flex-none pt-0.5 font-mono text-xs tabular-nums">{formatValue(it.value)}</span>}
                    {it.kind === "extrapolation" && (
                      <span className="flex-none pt-0.5 font-mono text-xs font-bold">{it.score === null ? "N/A" : (it.score > 0 ? "+" : "") + it.score}</span>
                    )}
                    <span className="w-14 flex-none pt-0.5 text-right">
                      <StatusText status={it.verification.status} />
                    </span>
                    <span className="w-7 flex-none pt-0.5 text-right">
                      <CertaintyPill c={it.certainty} />
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      ))}

      <section className="mt-6">
        <h2 className="mb-1.5 text-lg font-bold">
          <span className="mr-2.5 font-mono text-[13px]">{String(sections.length + 2).padStart(2, "0")}</span>What we could not determine
        </h2>
        <Holes />
      </section>
      <p className="mt-6 text-[13px] text-muted italic">{report.disclaimer}</p>
    </div>
  );
}

export function Holes() {
  const report = useRunStore((s) => s.report);
  const select = useRunStore((s) => s.select);
  const holes = report?.holes ?? [];
  if (!holes.length) return <div className="font-mono text-xs text-muted">No declared gaps{report?.status === "final" ? "." : " yet."}</div>;
  return (
    <ul className="border-y border-ink">
      {holes.map((h) => (
        <li key={h.hole_id} className="flex gap-3 border-b border-rule py-1.5 last:border-b-0">
          <span className="w-16 flex-none font-mono text-[11px] font-bold text-bad">{h.hole_id}</span>
          <span className="w-32 flex-none font-mono text-[11px]">{h.criterion ? human(h.criterion) : "—"}</span>
          <span className="flex-1 text-sm">{h.reason}</span>
          {h.item_id && (
            <button onClick={() => select({ type: "item", id: h.item_id! })} className="font-mono text-[11px] text-link hover:underline">
              {h.item_id}
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}
