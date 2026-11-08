"use client";

import { useState } from "react";
import type { Block, SummarySentence } from "@contract/types";
import { BAND_BG, CERTAINTY_HELP } from "@/lib/format";
import { useRunStore } from "@/lib/store";
import { ChartView, Gauge, Holes, Scorecard } from "./report";
import { btn, Label } from "./ui";

// Like an AI-detector page: every sentence is shaded by how well checked
// evidence supports it. Hover for the breakdown; click to follow it down.
function Sentence({ s }: { s: SummarySentence }) {
  const select = useRunStore((x) => x.select);
  const selected = useRunStore((x) => x.selected);
  const [hover, setHover] = useState(false);
  const c = s.certainty;
  const band = c?.band ?? "none";
  const on = selected?.type === "sentence" && selected.id === s.id;
  return (
    <span className="relative">
      <button
        onClick={() => select({ type: "sentence", id: s.id })}
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
        onFocus={() => setHover(true)}
        onBlur={() => setHover(false)}
        className={`inline animate-[fade-in_300ms_ease-out] px-0.5 text-left transition-colors print:bg-transparent ${BAND_BG[band]} ${on ? "outline-2 outline-ink" : ""} ${
          s.verification.status === "pending" ? "animate-pulse" : ""
        }`}
      >
        {s.text}
      </button>{" "}
      {hover && (
        <span role="tooltip" className="absolute top-full left-0 z-30 mt-1 w-80 bg-desk px-3 py-2 font-mono text-[11px] leading-snug text-desk-ink shadow-lg print:hidden">
          <span className="text-white">
            {s.id} · certainty {c?.score ?? "—"} ({band})
          </span>
          <br />
          {c && (
            <>
              {c.basis}
              <br />
              support {c.support.toFixed(2)} × inherited {c.inherited.toFixed(2)} × completeness {c.completeness.toFixed(2)}
              <br />
            </>
          )}
          <span className="text-desk-muted">From {s.derived_from.join(", ") || "—"}. Click to follow it down.</span>
        </span>
      )}
    </span>
  );
}

function BlockView({ b }: { b: Block }) {
  const items = useRunStore((s) => s.items);
  const rollup = useRunStore((s) => s.report?.rollup ?? null);
  if (b.type === "paragraph")
    return (
      <p className="mb-2.5 text-justify text-[15px] leading-relaxed">
        {b.sentences.map((s) => (
          <Sentence key={s.id} s={s} />
        ))}
      </p>
    );
  if (b.type === "bullets")
    return (
      <ul className="mb-2.5 ml-5 list-disc text-[15px] leading-relaxed">
        {b.sentences.map((s) => (
          <li key={s.id}>
            <Sentence s={s} />
          </li>
        ))}
      </ul>
    );
  if (b.type === "chart") {
    const ch = items[b.chart_id];
    return (
      <figure className="mb-3 break-inside-avoid">
        {ch && ch.kind === "chart" ? <ChartView chart={ch} compact /> : <div className="font-mono text-xs text-muted italic">{b.chart_id} (missing)</div>}
        <figcaption className="text-[13px]">
          <Sentence s={b.caption} />
        </figcaption>
      </figure>
    );
  }
  return (
    <div className="mb-3 break-inside-avoid">
      <Scorecard rollup={rollup} />
    </div>
  );
}

export function BriefModule() {
  const summary = useRunStore((s) => s.summary);
  if (!summary) return null;
  if (summary.status === "not_started")
    return (
      <div>
        <Label>STAGES SUMMARIZE · CHECK BRIEF</Label>
        <h1 className="mt-0.5 text-[26px]">Brief</h1>
        <p className="mt-2 text-[15px] text-muted">The 3-page brief is written after the agent report is verified. Every sentence will cite the report.</p>
      </div>
    );
  return (
    <div className="animate-[fade-in_200ms_ease-out]">
      <div className="mb-3 flex flex-wrap items-center gap-3 print:hidden">
        <Label>
          STAGES SUMMARIZE · CHECK BRIEF · {summary.status.toUpperCase()} · {summary.stats.words} WORDS · ~{summary.stats.est_pages} PAGES
        </Label>
        <div className="ml-auto flex items-center gap-2 font-mono text-[10px]">
          {(["high", "medium", "low", "none"] as const).map((b) => (
            <span key={b} className={`${BAND_BG[b]} px-1.5`}>
              {b}
            </span>
          ))}
          <span className="text-muted" title={CERTAINTY_HELP}>
            = support by checked evidence
          </span>
          <button className={btn} onClick={() => window.print()}>
            PRINT / PDF
          </button>
        </div>
      </div>

      <article id="brief" className="mx-auto max-w-[8.5in] bg-white px-[0.6in] py-[0.5in] shadow-[0_0_0_1px_#D0D0D0] print:max-w-none print:p-0 print:shadow-none">
        <header className="border-b border-ink pb-3">
          <h1 className="text-[24px] leading-tight">{summary.title}</h1>
          <div className="mt-1 text-sm">
            {summary.company}
            {summary.ticker ? ` (${summary.ticker})` : ""} · as of {new Date(summary.as_of).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" })}
          </div>
          <div className="mt-3">
            <Gauge c={summary.certainty} label="BRIEF" />
          </div>
        </header>
        {summary.sections.map((sec, i) => (
          <section key={sec.heading || i} className="mt-4 break-inside-avoid-page">
            <h2 className="mb-1.5 text-[17px] font-bold">{sec.heading}</h2>
            {sec.blocks.map((b, k) => (
              <BlockView key={k} b={b} />
            ))}
          </section>
        ))}
        <section className="mt-4">
          <h2 className="mb-1.5 text-[17px] font-bold">What we could not determine</h2>
          <Holes />
        </section>
        <p className="mt-4 text-[12px] text-muted italic">{summary.disclaimer}</p>
      </article>
    </div>
  );
}
