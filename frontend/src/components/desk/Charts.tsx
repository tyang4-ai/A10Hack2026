"use client";

import type { ChartSpec, Claim } from "@/lib/api";
import { isFailed } from "@/lib/ui";

// Renders the agent's chart specs. A chart only draws if every point's claim
// exists and passed verification; otherwise it is withheld. Values always come
// from the verified claim, never from the spec.

const num = (v: string | null) => (v === null ? NaN : parseFloat(v.replace(/[^0-9.+-]/g, "")));
const fmt = (n: number, signed = false) => (signed && n > 0 ? "+" : "") + (Math.round(n * 10) / 10).toFixed(1).replace(/\.0$/, "");

export function resolveChart(spec: ChartSpec, byId: Map<string, Claim>) {
  const pts = spec.points.map((p) => ({ ...p, claim: byId.get(p.claim_id) }));
  const bad = pts.filter((p) => !p.claim || isFailed(p.claim) || p.claim.agent_check.verdict !== "supported" || isNaN(num(p.claim.value)));
  return { pts: pts.map((p) => ({ ...p, v: num(p.claim?.value ?? null) })), bad };
}

type Pt = ReturnType<typeof resolveChart>["pts"][number];

const AXIS = "fill-muted font-mono text-[10px]";

function YAxis({ ticks, y, x0, x1 }: { ticks: number[]; y: (v: number) => number; x0: number; x1: number }) {
  return (
    <g>
      {ticks.map((t) => (
        <g key={t}>
          <line x1={x0} x2={x1} y1={y(t)} y2={y(t)} stroke={t === ticks[0] ? "#000" : "#E2E2E2"} />
          <text x={x0 - 6} y={y(t) + 3} textAnchor="end" className={AXIS}>
            {t}
          </text>
        </g>
      ))}
    </g>
  );
}

function LineChart({ pts, onPick }: { pts: Pt[]; onPick: (id: string) => void }) {
  const hi = Math.ceil(Math.max(...pts.map((p) => p.v)) / 2) * 2 + 2;
  const y = (v: number) => 150 - (v / hi) * 130;
  const x = (i: number) => 60 + (i * 280) / Math.max(1, pts.length - 1);
  const ticks = Array.from({ length: hi / 2 + 1 }, (_, i) => i * 2);
  return (
    <svg viewBox="0 0 360 170" className="block w-full font-mono text-[10px]" role="img" aria-label={pts.map((p) => `${p.label} ${p.v}`).join(", ")}>
      <YAxis ticks={ticks} y={y} x0={34} x1={350} />
      <polyline points={pts.map((p, i) => `${x(i)},${y(p.v)}`).join(" ")} fill="none" stroke="#000" strokeWidth={2} />
      {pts.map((p, i) => (
        <g key={p.claim_id} onClick={() => onPick(p.claim_id)} className="cursor-pointer">
          <circle cx={x(i)} cy={y(p.v)} r={i === pts.length - 1 ? 4.5 : 3.5} fill={i === pts.length - 1 ? "#000" : "#fff"} stroke="#000" strokeWidth={1.5} />
          <text x={x(i)} y={y(p.v) - 9} textAnchor="middle" fontWeight={i === pts.length - 1 ? 700 : 400}>
            {fmt(p.v)}
          </text>
          <text x={x(i)} y={166} textAnchor="middle" className={AXIS}>
            {p.label}
          </text>
        </g>
      ))}
    </svg>
  );
}

function Waterfall({ pts, onPick }: { pts: Pt[]; onPick: (id: string) => void }) {
  let run = 0;
  const bars = pts.map((p) => {
    if (p.role === "delta") {
      const from = run;
      run += p.v;
      return { p, from, to: run };
    }
    run = p.v;
    return { p, from: null as number | null, to: p.v };
  });
  const vals = bars.flatMap((b) => [b.from ?? b.to, b.to]);
  const lo = Math.floor(Math.min(...vals) - 1.5);
  const hi = Math.ceil(Math.max(...vals) + 0.5);
  const y = (v: number) => 150 - ((v - lo) / (hi - lo)) * 130;
  const w = 44;
  const gap = (412 - 40 - bars.length * w) / Math.max(1, bars.length - 1);
  const x = (i: number) => 40 + i * (w + gap);
  const ticks = [lo, Math.round((lo + hi) / 2), hi];
  return (
    <svg viewBox="0 0 420 170" className="block w-full font-mono text-[10px]" role="img" aria-label="Margin bridge">
      <YAxis ticks={ticks} y={y} x0={30} x1={412} />
      <path d="M24 139 l6 -4 M24 144 l6 -4" stroke="#000" />
      {bars.map(({ p, from, to }, i) => {
        const delta = from !== null;
        const top = delta ? Math.max(from, to) : to;
        const bottom = delta ? Math.min(from, to) : lo;
        const fill = !delta ? "#000" : to >= from ? "#007A3D" : "#C00000";
        return (
          <g key={p.claim_id} onClick={() => onPick(p.claim_id)} className="cursor-pointer">
            <rect x={x(i)} y={y(top)} width={w} height={Math.max(1, y(bottom) - y(top))} fill={fill} />
            {i < bars.length - 1 && <line x1={x(i) + w} x2={x(i + 1)} y1={y(to)} y2={y(to)} stroke="#000" strokeDasharray="2 2" />}
            <text x={x(i) + w / 2} y={delta && to < from ? y(bottom) + 12 : y(top) - 5} textAnchor="middle" fill={delta ? fill : "#000"} fontWeight={p.role === "total" ? 700 : 400}>
              {fmt(delta ? p.v : to, delta)}
            </text>
            <text x={x(i) + w / 2} y={166} textAnchor="middle" className={AXIS}>
              {p.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function Fan({ pts, onPick }: { pts: Pt[]; onPick: (id: string) => void }) {
  const get = (r: string) => pts.find((p) => p.role === r)!;
  const [s, bull, base, bear] = [get("start"), get("bull"), get("base"), get("bear")];
  const hi = Math.ceil(Math.max(bull.v, s.v) / 2) * 2 + 2;
  const y = (v: number) => 150 - (v / hi) * 130;
  const ticks = Array.from({ length: hi / 2 + 1 }, (_, i) => i * 2);
  const end = 300;
  return (
    <svg viewBox="0 0 360 170" className="block w-full font-mono text-[10px]" role="img" aria-label={`From ${s.v} to bear ${bear.v}, base ${base.v}, bull ${bull.v}`}>
      <YAxis ticks={ticks} y={y} x0={34} x1={350} />
      <polygon points={`60,${y(s.v)} ${end},${y(bull.v)} ${end},${y(bear.v)}`} fill="#EDEDED" />
      {[bull, base, bear].map((p) => (
        <g key={p.claim_id} onClick={() => onPick(p.claim_id)} className="cursor-pointer">
          <line x1={60} y1={y(s.v)} x2={end} y2={y(p.v)} stroke="#000" strokeWidth={p === base ? 2 : 1} strokeDasharray={p === base ? undefined : "4 3"} />
          <text x={end + 6} y={y(p.v) + 3}>
            {p.label.toUpperCase()} {fmt(p.v)}
          </text>
        </g>
      ))}
      <circle cx={60} cy={y(s.v)} r={4.5} fill="#000" onClick={() => onPick(s.claim_id)} className="cursor-pointer" />
      <text x={60} y={y(s.v) - 9} textAnchor="middle">
        {fmt(s.v)}
      </text>
      <text x={60} y={166} textAnchor="middle" className={AXIS}>
        {s.label}
      </text>
      <text x={end} y={166} textAnchor="middle" className={AXIS}>
        Next FY
      </text>
    </svg>
  );
}

const GREYS = ["#000000", "#555555", "#8C8C8C", "#BDBDBD", "#FFFFFF"];

function Stacked({ pts, onPick }: { pts: Pt[]; onPick: (id: string) => void }) {
  const total = pts.reduce((n, p) => n + p.v, 0) || 1;
  return (
    <div>
      <div role="img" aria-label={pts.map((p) => `${p.label} ${p.v}%`).join(", ")} className="mt-2.5 flex h-[26px] border border-ink">
        {pts.map((p, i) => (
          <button key={p.claim_id} aria-label={`${p.label} ${p.v}%`} onClick={() => onPick(p.claim_id)} className="h-full transition-[width] duration-500" style={{ width: `${(p.v / total) * 100}%`, background: GREYS[i % 5] }} />
        ))}
      </div>
      <table className="mt-2.5 w-full font-mono text-[11px]">
        <tbody>
          {pts.map((p, i) => (
            <tr key={p.claim_id} onClick={() => onPick(p.claim_id)} className="cursor-pointer border-t border-[#E2E2E2] hover:bg-zebra">
              <td className="py-0.5">
                <span className="mr-1.5 inline-block h-[9px] w-[9px] border border-ink" style={{ background: GREYS[i % 5] }} />
                {p.label}
              </td>
              <td className="text-right tabular-nums">{fmt(p.v)}%</td>
              <td className="pl-3 text-right text-muted">{p.claim_id}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Chart({ spec, n, byId, onPick }: { spec: ChartSpec; n: number; byId: Map<string, Claim>; onPick: (id: string) => void }) {
  const { pts, bad } = resolveChart(spec, byId);
  const Body = { line: LineChart, waterfall: Waterfall, fan: Fan, stacked_bar: Stacked }[spec.type];
  return (
    <figure className="m-0 animate-[fade-in_300ms_ease-out]">
      <div className="flex justify-between font-mono text-[10px] tracking-[0.12em] text-muted">
        <span>
          {spec.title.toUpperCase()} · {spec.unit}
        </span>
        <span>⟨{spec.type}⟩</span>
      </div>
      {bad.length ? (
        <div className="mt-1 border border-dashed border-bad px-3 py-2.5 font-mono text-[11px] text-bad">
          <div className="font-bold tracking-[0.08em]">WITHHELD · {spec.type.toUpperCase()}</div>
          <div>Unverified points: {bad.map((p) => p.claim_id).join(", ")}</div>
        </div>
      ) : (
        <div className="mt-1">
          <Body pts={pts} onPick={onPick} />
        </div>
      )}
      <figcaption className="text-[13px]">
        <strong>Figure {n}.</strong> {spec.caption}
      </figcaption>
      <div className="mt-0.5 font-mono text-[10px]">
        {spec.lint.map((l, i) => (
          <span key={l.check} className={l.ok ? "text-good" : ""}>
            {i > 0 && " · "}
            {l.ok ? "✓" : "!"} {l.check.toUpperCase()}
          </span>
        ))}
      </div>
    </figure>
  );
}
