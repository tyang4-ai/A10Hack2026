import type { CertaintyBand, Stage, Value, VerificationStatus } from "@contract/types";

// Round half up. toFixed rounds 5.85 to 5.8 because of float error, so shift
// by a tiny epsilon first (docs/CONTRACT.md section 11).
export function roundHalfUp(n: number, digits = 1): number {
  const f = 10 ** digits;
  return Math.sign(n) * Math.round(Math.abs(n) * f + 1e-9) / f;
}

const SCALE_WORD = { one: "", thousand: "thousand", million: "million", billion: "billion" } as const;
const CURRENCY: Record<string, string> = { USD: "$", EUR: "€", GBP: "£", JPY: "¥" };

// Format a reported value from its own unit and scale. Never assume a currency.
export function formatValue(v: Value | null): string {
  if (!v) return "—";
  const n = roundHalfUp(v.number, 1).toLocaleString("en-US", { maximumFractionDigits: 1 });
  if (v.unit === "percent" || v.unit === "%") return `${n}%`;
  const scale = SCALE_WORD[v.scale] ? ` ${SCALE_WORD[v.scale]}` : "";
  const cur = CURRENCY[v.unit];
  return cur ? `${cur}${n}${scale}` : `${n}${scale} ${v.unit}`.trim();
}

export function formatAxis(n: number, unit: string, scale: Value["scale"]): string {
  const cur = CURRENCY[unit] ?? "";
  const s = scale === "million" ? "m" : scale === "billion" ? "bn" : scale === "thousand" ? "k" : "";
  return `${cur}${roundHalfUp(n, 1).toLocaleString("en-US")}${s}`;
}

export const usd = (n: number) => `$${n < 1 ? n.toFixed(4) : n.toFixed(2)}`;
export const fmtTime = (iso: string) => new Date(iso).toLocaleTimeString("en-GB", { hour12: false });

export const STAGES: Stage[] = ["plan", "approve", "collect", "extract", "draft", "verify", "repair", "summarize", "verify_summary", "complete"];
export const STAGE_LABEL: Record<Stage, string> = {
  plan: "PLAN",
  approve: "APPROVE",
  collect: "COLLECT",
  extract: "EXTRACT",
  draft: "DRAFT",
  verify: "VERIFY",
  repair: "REPAIR",
  summarize: "SUMMARIZE",
  verify_summary: "CHECK BRIEF",
  complete: "COMPLETE",
};

export type Module = "sources" | "report" | "brief";
export const moduleFor = (s: Stage): Module =>
  s === "plan" || s === "approve" || s === "collect" ? "sources" : s === "summarize" || s === "verify_summary" || s === "complete" ? "brief" : "report";

// Certainty bands. Light backgrounds for the brief, text colours elsewhere.
export const BAND_BG: Record<CertaintyBand, string> = {
  high: "bg-[#DDF3E6]",
  medium: "bg-[#FFEFC7]",
  low: "bg-[#FADADA]",
  none: "bg-[#E9E9E9]",
};
export const BAND_TEXT: Record<CertaintyBand, string> = {
  high: "text-good",
  medium: "text-[#8A5A00]",
  low: "text-bad",
  none: "text-muted",
};

export const STATUS_TEXT: Record<VerificationStatus, string> = {
  pending: "text-muted",
  pass: "text-good",
  fixed: "text-[#1A6FB0]",
  fail: "text-bad",
  blocked: "text-[#8A5A00]",
};

export const CERTAINTY_HELP = "How well this is supported by checked evidence. Not the probability it is true.";

export const human = (s: string) => s.replace(/_/g, " ");
