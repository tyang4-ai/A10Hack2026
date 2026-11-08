"use client";

import type { Certainty, Item, SourceStatus, VerificationStatus } from "@contract/types";
import { BAND_TEXT, CERTAINTY_HELP, STATUS_TEXT } from "@/lib/format";

export const KIND_LETTER: Record<Item["kind"], string> = { data: "D", conclusion: "C", extrapolation: "E", chart: "G" };
const KIND_NAME: Record<Item["kind"], string> = { data: "Data", conclusion: "Conclusion", extrapolation: "Score", chart: "Chart" };

export function KindBadge({ kind }: { kind: Item["kind"] }) {
  return (
    <span title={KIND_NAME[kind]} aria-label={KIND_NAME[kind]} className="inline-grid h-5 w-5 flex-none place-items-center rounded-[3px] border border-current font-mono text-[10px] font-semibold">
      {KIND_LETTER[kind]}
    </span>
  );
}

// Status is a word in its colour, never colour alone.
export function StatusText({ status }: { status: VerificationStatus }) {
  return <span className={`font-mono text-[11px] font-semibold ${STATUS_TEXT[status]}`}>{status.toUpperCase()}</span>;
}

export function CertaintyPill({ c }: { c: Certainty | null }) {
  if (!c || c.score === null)
    return (
      <span className="font-mono text-[11px] text-muted" title={CERTAINTY_HELP}>
        —
      </span>
    );
  return (
    <span title={`Certainty ${c.score} (${c.band}). ${CERTAINTY_HELP}\n${c.basis}`} className={`font-mono text-[11px] font-semibold tabular-nums ${BAND_TEXT[c.band]}`}>
      {c.score}
    </span>
  );
}

const SOURCE_STATUS_CLS: Partial<Record<SourceStatus, string>> = {
  fetching: "text-link animate-pulse",
  extracting: "text-link animate-pulse",
  fetched: "text-good",
  extracted: "text-good",
  failed: "text-bad",
  blocked: "text-bad",
  revoked: "text-bad",
  quarantined: "rounded-[3px] bg-warn px-1 text-white",
  skipped: "text-muted",
};

export function SourceChip({ status }: { status: SourceStatus }) {
  return <span className={`font-mono text-[11px] font-semibold ${SOURCE_STATUS_CLS[status] ?? "text-muted"}`}>{status.toUpperCase()}</span>;
}

export function Chip({ children, title }: { children: React.ReactNode; title?: string }) {
  return (
    <span title={title} className="rounded-[3px] bg-zebra px-1.5 py-px text-[11px] text-muted outline outline-1 outline-rule">
      {children}
    </span>
  );
}

export function Label({ children }: { children: React.ReactNode }) {
  return <div className="text-[11px] font-medium text-muted">{children}</div>;
}

export function Missing({ id }: { id: string }) {
  return <span className="font-mono text-[11px] text-muted italic">{id} (missing)</span>;
}

export function Skeleton({ rows = 6 }: { rows?: number }) {
  return (
    <div aria-busy="true" aria-label="Loading" className="grid gap-2">
      <div className="h-6 w-56 rounded-[3px] bg-zebra" />
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="h-5 rounded-[3px] bg-zebra" style={{ width: `${92 - ((i * 13) % 30)}%` }} />
      ))}
    </div>
  );
}

const base = "inline-flex items-center justify-center gap-1.5 rounded-[4px] text-[13px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40";
export const btn = `${base} h-8 border border-[#CBD5E1] bg-white px-3 text-ink hover:border-ink disabled:hover:border-[#CBD5E1]`;
export const btnSm = `${base} h-7 border border-[#CBD5E1] bg-white px-2 text-[12px] text-ink hover:border-ink`;
export const btnPrimary = `${base} h-8 bg-ink px-3.5 text-white hover:bg-[#1E293B]`;
export const btnDanger = `${base} h-7 border border-bad/50 bg-white px-2 text-[12px] text-bad hover:bg-bad hover:text-white`;
