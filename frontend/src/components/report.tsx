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
        <Label>{label} certainty</Label>
        <div className="font-mono text-[28px] font-medium tabular-nums">{c.mean ?? "—"}</div>
        <div className="mt-1 flex h-2 w-48 overflow-hidden">
          {segs.map(([b, v]) => (
            <div key={b} className={`${BAND_BG[b]} border-r border-white last:border-r-0`} style={{ width: `${(v / total) * 100}%` }} />
          ))}
        </div>
        <div className="mt-1 text-[11px] text-muted">{c.high_pct}% in the high band</div>
      </div>
      <div className="border-l border-rule pl-6">
        <Label>Coverage</Label>
        <div className="font-mono text-[28px] font-medium tabular-nums">
          {c.coverage_scored} <span className="text-base text-muted">of {c.coverage_in_scope}</span>
        </div>
        <div className="mt-1 text-[11px] text-muted">screen criteria scored</div>
      </div>
    </div>
  );
}

export function Scorecard({ rollup }: { rollup: Rollup | null }) {
  const items = useRunStore((s) => s.items);
  const select = useRunStore((s) => s.select);
  if (!rollup) return <div className="text-muted">Scorecard pending</div>;
  const scores = Object.values(items).filter((i) => i.kind === "extrapolation");
  const fmt = (n: number | null) => (n === null ? "N/A" : `${n > 0 ? "+" : ""}${roundHalfUp(n, 2).toFixed(2)}`);
  return (
    <div>
      <div className="flex flex-wrap items-end gap-x-8 gap-y-2">
        <div>
          <Label>Weighted average (lead with this)</Label>
          <div className="font-mono text-[28px] font-medium tabular-nums">{fmt(rollup.weighted_average)}</div>
        </div>
        {(rollup.normalized_pct !== null || rollup.percent_of_max_pct !== null) && (
          <div>
            <Label>As a percentage (inferred)</Label>
            <div className="text-[13px]">
              <span className="font-mono tabular-nums">{rollup.normalized_pct}%</span> normalized, <span className="font-mono tabular-nums">{rollup.percent_of_max_pct}%</span> of the maximum
            </div>
          </div>
        )}
        <div>
          <Label>Coverage</Label>
          <div className="text-[13px]">
            <span className="font-mono tabular-nums">
              {rollup.scored} of {rollup.in_scope}
            </span>{" "}
            criteria scored
            {rollup.na_criteria.length > 0 && <span className="text-muted">, not available: {rollup.na_criteria.map(human).join(", ")}</span>}
          </div>
        </div>
      </div>
      <table className="mt-3 w-full border-y border-ink text-[13px]">
        <thead>
          <tr className="border-b border-ink text-left text-[11px] text-muted">
            <th className="py-1.5 font-medium">Category</th>
            <th className="text-right font-medium">Weight</th>
            <th className="text-right font-medium">Score</th>
            <th className="pr-4 text-right font-medium">Scored</th>
            <th className="font-medium">Criteria (score, certainty)</th>
          </tr>
        </thead>
        <tbody>
          {rollup.categories.map((c) => (
            <tr key={c.category} className="border-t border-rule align-top">
              <td className="py-1.5 capitalize">{human(c.category)}</td>
              <td className="text-right font-mono tabular-nums">{c.weight}</td>
              <td className={`text-right font-mono font-semibold tabular-nums ${c.score === null ? "text-muted" : ""}`}>{fmt(c.score)}</td>
              <td className="pr-4 text-right font-mono tabular-nums">
                {c.scored}/{c.in_scope}
              </td>
              <td className="py-1">
                <div className="flex flex-wrap gap-x-4 gap-y-0.5">
                  {scores
                    .filter((e) => e.category === c.category)
                    .map((e) =>
                      e.kind === "extrapolation" ? (
                        <button key={e.id} onClick={() => select({ type: "item", id: e.id })} className="flex items-baseline gap-1.5 hover:underline">
                          <span>{human(e.criterion)}</span>
                          <span className="font-mono font-semibold">{e.score === null ? "N/A" : (e.score > 0 ? "+" : "") + e.score}</span>
                          <CertaintyPill c={e.certainty} />
                        </button>
                      ) : null,
                    )}
                </div>
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
// Series fills, from the theme's tokens: ink first, then link, muted and warn.
const SERIES_FILL = ["fill-ink", "fill-link", "fill-muted", "fill-warn"];
const SERIES_STROKE = ["stroke-ink", "stroke-link", "stroke-muted", "stroke-warn"];

// Split a category label into at most two lines that fit `chars` each; the full label is in a <title>.
function labelLines(label: string, chars: number): string[] {
  if (label.length <= chars) return [label];
  const words = label.split(" ");
  let first = "";
  while (words.length && (first + " " + words[0]).trim().length <= chars) first = (first + " " + words.shift()).trim();
  if (!first) first = words.shift() ?? "";
  const rest = words.join(" ");
  return [first.length > chars ? first.slice(0, chars - 1) + "…" : first, rest.length > chars ? rest.slice(0, chars - 1) + "…" : rest].filter(Boolean);
}

// Bar and line charts that handle negative values (bars hang below a zero line), several
// series (grouped bars with a legend, or one line each) and compact labels (€15.7bn).
export function ChartView({ chart, compact }: { chart: Chart; compact?: boolean }) {
  const select = useRunStore((s) => s.select);
  const pts = chart.series.flatMap((s, si) => s.points.map((p) => ({ ...p, series: s.name, si })));
  const fmt = (v: number) => formatAxis(v, chart.unit, chart.scale);
  if (chart.chart_type === "table" || pts.length === 0)
    return (
      <table className="w-full text-[13px]">
        <tbody>
          {pts.map((p) => (
            <tr key={p.series + p.x} className="border-t border-rule">
              <td>{p.x}</td>
              {chart.series.length > 1 && <td className="text-muted">{p.series}</td>}
              <td className="text-right font-mono tabular-nums">{fmt(p.y)}</td>
              <td className="pl-2 text-right font-mono text-muted">{p.ref}</td>
            </tr>
          ))}
        </tbody>
      </table>
    );

  const xs = [...new Set(pts.map((p) => p.x))];
  const nSeries = chart.series.length;
  const multi = nSeries > 1;
  // The value range always includes zero, with headroom on the side away from it for labels.
  const hiRaw = Math.max(0, ...pts.map((p) => p.y));
  const loRaw = Math.min(0, ...pts.map((p) => p.y));
  const span = hiRaw - loRaw || 1;
  const hi = hiRaw + (hiRaw > 0 ? span * 0.14 : 0);
  const lo = loRaw - (loRaw < 0 ? span * 0.14 : 0);
  const longest = Math.max(...xs.map((x) => x.length));
  const W = 560;
  const left = 64;
  const right = 10;
  const top = 12;
  const slot = (W - left - right) / xs.length;
  const chars = Math.max(6, Math.floor(slot / 6.2));
  const labelRows = longest > chars ? 2 : 1;
  const H = (compact ? 160 : 190) + (labelRows - 1) * 12;
  const bottom = H - 10 - labelRows * 12;
  const y = (v: number) => top + ((hi - v) / (hi - lo)) * (bottom - top);
  const cx = (i: number) => left + (i + 0.5) * slot;
  const group = Math.min(slot * 0.72, multi ? 30 * nSeries : 48);
  const bw = group / nSeries;
  const bx = (i: number, si: number) => cx(i) - group / 2 + si * bw;
  const ticks = [...new Set([lo, 0, hi].map((t) => (Math.abs(t) < 1e-9 ? 0 : t)))];
  // Value labels only when each bar has room for one.
  const showValues = !multi || bw >= 28;

  return (
    <figure className="m-0">
      {multi && (
        <div className="mb-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-muted">
          {chart.series.map((s, si) => (
            <span key={s.name} className="flex items-center gap-1">
              <svg aria-hidden width="10" height="10">
                <rect width="10" height="10" className={SERIES_FILL[si % SERIES_FILL.length]} />
              </svg>
              {s.name}
            </span>
          ))}
        </div>
      )}
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="block w-full font-mono text-[10px]"
        role="img"
        aria-label={`${chart.title}: ${pts.map((p) => `${multi ? `${p.series} ` : ""}${p.x} ${fmt(p.y)}`).join(", ")}`}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line x1={left} x2={W - right} y1={y(t)} y2={y(t)} stroke={t === 0 ? "var(--color-ink)" : "var(--color-rule)"} />
            <text x={left - 6} y={y(t) + 3} textAnchor="end" className="fill-muted">
              {fmt(t)}
            </text>
          </g>
        ))}
        {chart.chart_type === "line" &&
          chart.series.map((s, si) => (
            <polyline
              key={s.name}
              points={s.points.map((p) => `${cx(xs.indexOf(p.x))},${y(p.y)}`).join(" ")}
              fill="none"
              className={SERIES_STROKE[si % SERIES_STROKE.length]}
              strokeWidth={2}
            />
          ))}
        {pts.map((p) => {
          const i = xs.indexOf(p.x);
          const neg = p.y < 0;
          const labelY = neg ? y(p.y) + 12 : y(p.y) - 5;
          return (
            <g key={p.series + p.x} onClick={() => select({ type: "item", id: p.ref })} className="cursor-pointer">
              <title>{`${multi ? `${p.series}, ` : ""}${p.x}: ${fmt(p.y)} (${p.ref})`}</title>
              {chart.chart_type === "bar" ? (
                <rect
                  x={bx(i, p.si) + 1}
                  y={Math.min(y(0), y(p.y))}
                  width={Math.max(1, bw - 2)}
                  height={Math.max(1, Math.abs(y(p.y) - y(0)))}
                  className={`${SERIES_FILL[p.si % SERIES_FILL.length]} hover:fill-link`}
                />
              ) : (
                <circle cx={cx(i)} cy={y(p.y)} r={4} className={`fill-paper ${SERIES_STROKE[p.si % SERIES_STROKE.length]} hover:fill-link`} strokeWidth={1.5} />
              )}
              {showValues && (
                <text x={chart.chart_type === "bar" ? bx(i, p.si) + bw / 2 : cx(i)} y={labelY} textAnchor="middle" className="fill-ink">
                  {fmt(p.y)}
                </text>
              )}
            </g>
          );
        })}
        {xs.map((x, i) => (
          <text key={x} x={cx(i)} y={bottom + 14} textAnchor="middle" className="fill-muted">
            <title>{x}</title>
            {labelLines(x, chars).map((line, k) => (
              <tspan key={k} x={cx(i)} dy={k ? 12 : 0}>
                {line}
              </tspan>
            ))}
          </text>
        ))}
      </svg>
    </figure>
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
    <div className="">
            <h1 className="text-[22px] font-semibold">Agent report</h1>
      <p className="mt-1 max-w-[72ch] text-[14px] text-muted">Every number is quoted from a stored source and checked by code.</p>
      <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-[13px]">
        <div className="flex gap-1.5"><dt className="text-muted">Status</dt><dd className="font-medium capitalize">{report.status.replace("_", " ")}</dd></div>
        <div className="flex gap-1.5"><dt className="text-muted">Items</dt><dd className="font-mono tabular-nums">{list.length}</dd></div>
        <div className="flex gap-1.5"><dt className={STATUS_TEXT.pass}>Pass</dt><dd className="font-mono tabular-nums">{status("pass")}</dd></div>
        <div className="flex gap-1.5"><dt className={STATUS_TEXT.fixed}>Fixed</dt><dd className="font-mono tabular-nums">{status("fixed")}</dd></div>
        <div className="flex gap-1.5"><dt className={STATUS_TEXT.fail}>Fail</dt><dd className="font-mono tabular-nums">{status("fail")}</dd></div>
        <div className="flex gap-1.5"><dt className="text-muted">Pending</dt><dd className="font-mono tabular-nums">{status("pending")}</dd></div>
      </dl>
      <div className="mt-5">
        <Gauge c={report.certainty} label="Agent report" />
      </div>

      <section className="mt-6">
        <h2 className="mb-2 text-[15px] font-semibold">Scorecard
        </h2>
        <Scorecard rollup={report.rollup} />
      </section>

      {sections.map((sec, k) => (
        <section key={sec.category} className="mt-6">
          <h2 className="mb-1.5 text-[15px] font-semibold">
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
                <li key={id} className={` border-b border-rule last:border-b-0 ${on ? "bg-zebra" : "hover:bg-zebra"}`}>
                  <button onClick={() => select({ type: "item", id })} className="flex w-full items-start gap-2 px-1 py-1.5 text-left">
                    <KindBadge kind={it.kind} />
                    <span className="w-12 flex-none pt-0.5 font-mono text-[11px] text-muted">{id}</span>
                    <span className={`min-w-0 flex-1 font-serif text-[15px] leading-snug ${it.verification.status === "fail" ? "text-bad line-through decoration-1" : ""}`}>{itemText(it)}</span>
                    {it.kind === "data" && <span className="flex-none pt-0.5 font-mono text-[12px] tabular-nums">{formatValue(it.value)}</span>}
                    {it.kind === "extrapolation" && (
                      <span className="flex-none pt-0.5 font-mono text-[12px] font-semibold">{it.score === null ? "N/A" : (it.score > 0 ? "+" : "") + it.score}</span>
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
        <h2 className="mb-1.5 text-[15px] font-semibold">What we could not determine
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
  if (!holes.length) return <div className="text-muted">No gaps</div>;
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
