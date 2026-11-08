"use client";

import type { Certainty, Item, SourceStatus, VerificationStatus } from "@contract/types";
import { BAND_TEXT, CERTAINTY_HELP, STATUS_TEXT } from "@/lib/format";

export const KIND_LETTER: Record<Item["kind"], string> = { data: "D", conclusion: "C", extrapolation: "E", chart: "G" };

export function KindBadge({ kind }: { kind: Item["kind"] }) {
  return (
    <span title={kind} className="inline-grid h-5 w-5 flex-none place-items-center border border-ink font-mono text-[10px] font-bold">
      {KIND_LETTER[kind]}
    </span>
  );
}

export function StatusText({ status }: { status: VerificationStatus }) {
  return <span className={`font-mono text-[11px] font-bold tracking-[0.06em] ${STATUS_TEXT[status]}`}>{status.toUpperCase()}</span>;
}

export function CertaintyPill({ c }: { c: Certainty | null }) {
  if (!c || c.score === null) return <span className="font-mono text-[11px] text-muted" title={CERTAINTY_HELP}>—</span>;
  return (
    <span title={`${CERTAINTY_HELP}\n${c.basis}`} className={`font-mono text-[11px] font-bold tabular-nums ${BAND_TEXT[c.band]}`}>
      {c.score}
    </span>
  );
}

const SOURCE_STATUS_CLS: Partial<Record<SourceStatus, string>> = {
  fetching: "text-[#1A6FB0] animate-pulse",
  extracting: "text-[#1A6FB0] animate-pulse",
  fetched: "text-good",
  extracted: "text-good",
  failed: "text-bad",
  blocked: "text-bad",
  revoked: "text-bad",
  quarantined: "bg-[#8A5A00] text-white px-1",
  skipped: "text-muted",
};

export function SourceChip({ status }: { status: SourceStatus }) {
  return <span className={`font-mono text-[10px] font-bold tracking-[0.08em] ${SOURCE_STATUS_CLS[status] ?? "text-muted"}`}>{status.toUpperCase()}</span>;
}

export function Label({ children }: { children: React.ReactNode }) {
  return <div className="font-mono text-[10px] tracking-[0.12em] text-muted">{children}</div>;
}

export function Missing({ id }: { id: string }) {
  return <span className="font-mono text-[11px] text-muted italic">{id} (missing)</span>;
}

export const btn =
  "border border-ink px-2.5 py-1 font-mono text-[11px] tracking-[0.06em] transition-colors hover:bg-ink hover:text-white disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-ink";
export const btnPrimary =
  "border border-ink bg-ink px-3 py-1.5 font-mono text-[11px] font-bold tracking-[0.08em] text-white transition-opacity hover:opacity-85 disabled:opacity-30";
